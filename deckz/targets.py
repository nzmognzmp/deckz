from collections import defaultdict
from logging import getLogger
from pathlib import Path
from typing import (
    Any,
    DefaultDict,
    Dict,
    FrozenSet,
    Iterable,
    Iterator,
    List,
    Optional,
    Set,
    Tuple,
    Union,
)

from attr import attrib, Attribute, attrs, Factory
from yaml import safe_load as yaml_safe_load

from deckz.exceptions import DeckzException
from deckz.paths import Paths


_logger = getLogger(__name__)


@attrs(auto_attribs=True)
class Title:
    title: str
    level: int


Content = str
ContentOrTitle = Union[Content, Title]


@attrs(auto_attribs=True)
class Part:
    title: Optional[str]
    sections: List[ContentOrTitle] = Factory(list)


@attrs(auto_attribs=True)
class Dependencies:
    used: Set[Path] = Factory(set)
    missing: Set[str] = Factory(set)
    unused: Set[Path] = Factory(set)

    def update(self, other: "Dependencies") -> None:
        self.used |= other.used
        self.missing |= other.missing
        self.unused |= other.unused - self.used
        self.unused -= other.used

    @staticmethod
    def merge(*dependencies_list: "Dependencies") -> "Dependencies":
        dependencies = Dependencies()
        for ds in dependencies_list:
            dependencies.used |= ds.used
            dependencies.missing |= ds.missing
            dependencies.unused |= ds.unused - dependencies.used
            dependencies.unused -= ds.used
        return dependencies

    def merge_dicts(
        *dependencies_dicts: Dict[str, "Dependencies"]
    ) -> Dict[str, "Dependencies"]:
        keys = set.union(*(set(d) for d in dependencies_dicts))
        merged_dict = {}
        for key in keys:
            merged_dict[key] = Dependencies.merge(
                *(d[key] for d in dependencies_dicts if key in d)
            )
        return merged_dict


@attrs(auto_attribs=True)
class Target:
    name: str
    dependencies: Dependencies
    parts: List[Part]
    section_dependencies: Dict[str, Dependencies]
    section_flavors: DefaultDict[str, Set[str]]

    @classmethod
    def from_targets(cls, targets: Iterable["Target"], name: str) -> "Target":
        dependencies = Dependencies.merge(*(t.dependencies for t in targets))
        parts = [p for t in targets for p in t.parts]
        section_dependencies = Dependencies.merge_dicts(
            *(t.section_dependencies for t in targets)
        )
        section_flavors = defaultdict(set)
        for target in targets:
            for key, value in target.section_flavors.items():
                section_flavors[key].update(value)
        return Target(name, dependencies, parts, section_dependencies, section_flavors)


@attrs(auto_attribs=True)
class TargetBuilder:
    _data: Dict[str, Any]
    _paths: Paths
    _local_latex_dir: Path = attrib(init=False)

    def __attrs_post_init__(self) -> None:
        self._local_dir = self._paths.current_dir
        self._local_latex_dir = self._local_dir / "latex"

    def build(self) -> Target:
        all_dependencies = Dependencies()
        all_dependencies.unused.update(self._local_latex_dir.glob("**/*.tex"))
        all_items = []
        section_dependencies = {}
        section_flavors = defaultdict(set)
        for section_config in self._data["sections"]:
            if not isinstance(section_config, dict):
                section_config = dict(path=section_config)
            section_path = section_config["path"]
            section_flavors[section_path].add(section_config.get("flavor"))
            result = self._parse_section_dir(section_path, section_config, 0)
            if result is None:
                result = self._parse_section_file(section_path, section_config, 0)
            if result is not None:
                items, dependencies = result
                all_items.extend(items)
            else:
                dependencies = Dependencies()
                dependencies.missing.add(section_path)
            all_dependencies.update(dependencies)
            section_dependencies[section_path] = dependencies
        return Target(
            name=self._data["name"],
            dependencies=all_dependencies,
            parts=[Part(title=self._data["title"], sections=all_items)],
            section_dependencies=section_dependencies,
            section_flavors=section_flavors,
        )

    def _parse_section_dir(
        self, section_path_str: str, custom_config: Dict[str, Any], title_level: int
    ) -> Optional[Tuple[List[ContentOrTitle], Dependencies]]:
        section_path = Path(section_path_str)
        local_section_dir = self._local_latex_dir / section_path
        local_section_config_path = (local_section_dir / section_path).with_suffix(
            ".yml"
        )
        shared_section_dir = self._paths.shared_latex_dir / section_path
        shared_section_config_path = (
            shared_section_dir / section_path.parts[-1]
        ).with_suffix(".yml")
        dependencies = Dependencies()
        if local_section_config_path.exists():
            section_config_path = local_section_config_path
        elif shared_section_config_path.exists():
            section_config_path = shared_section_config_path
        else:
            return None
        section_config = yaml_safe_load(section_config_path.read_text(encoding="utf8"))
        title = (
            custom_config["title"]
            if "title" in custom_config
            else section_config["title"]
        )
        if "flavor" not in custom_config:
            raise DeckzException(
                f"Incorrect targets {self._paths.targets}. "
                f"Mandatory flavor not specified in {section_path} configuration."
            )
        flavor_name = custom_config["flavor"]
        if "flavors" not in section_config:
            raise DeckzException(
                f"Incorrect targets {self._paths.targets}. "
                f"Mandatory dictionary `flavors` not found in {section_config_path}."
            )
        flavors = section_config["flavors"]
        if flavor_name not in flavors:
            flavors_string = ", ".join("'%s'" % f for f in flavors)
            raise DeckzException(
                f"Incorrect targets {self._paths.targets}. "
                f"'{flavor_name}' not amongst available flavors: {flavors_string} "
                f"of {section_config_path}."
            )

        flavor = flavors[flavor_name]
        items: List[ContentOrTitle] = [Title(title=title, level=title_level)]
        default_titles = section_config.get("default_titles")
        for item in flavor:
            self._process_item(
                item=item,
                items=items,
                dependencies=dependencies,
                default_titles=default_titles,
                section_config=custom_config,
                local_section_dir=local_section_dir,
                shared_section_dir=shared_section_dir,
                section_path_str=section_path_str,
                title_level=title_level + 1,
            )
        return items, dependencies

    def _process_item(
        self,
        item: Union[str, Dict[str, str]],
        items: List[ContentOrTitle],
        dependencies: Dependencies,
        default_titles: Optional[Dict[str, str]],
        section_config: Dict[str, Any],
        local_section_dir: Path,
        shared_section_dir: Path,
        section_path_str: str,
        title_level: int,
    ) -> None:
        if isinstance(item, str):
            filename = item
            if default_titles is not None:
                title = default_titles.get(filename)
            else:
                title = None
        else:
            filename, title = next(iter(item.items()))
        if "excludes" in section_config and filename in section_config["excludes"]:
            return
        if filename.startswith("/"):
            local_path = (self._local_latex_dir / filename[1:]).with_suffix(".tex")
            shared_path = (self._paths.shared_latex_dir / filename[1:]).with_suffix(
                ".tex"
            )
        elif filename.startswith("$"):
            nested_items, nested_dependencies = self._parse_section_dir(
                f"{section_path_str}/{filename[1:]}", dict(flavor=title), title_level,
            )
            items.extend(nested_items)
            dependencies.update(nested_dependencies)
            return
        else:
            local_path = (local_section_dir / filename).with_suffix(".tex")
            shared_path = (shared_section_dir / filename).with_suffix(".tex")
        if title is not None:
            items.append(Title(title=title, level=title_level))
        local_relative_path = local_path.relative_to(self._local_dir)
        shared_relative_path = shared_path.relative_to(self._paths.shared_dir)
        if local_path.exists():
            items.append(str(local_relative_path.with_suffix("")))
            dependencies.used.add(local_path.resolve())
        elif shared_path.exists():
            items.append(str(shared_relative_path.with_suffix("")))
            dependencies.used.add(shared_path.resolve())
        else:
            dependencies.missing.add(filename)

    def _parse_section_file(
        self, section_path: str, config: Dict[str, Any], title_level: int
    ) -> Optional[Tuple[List[ContentOrTitle], Dependencies]]:
        local_section_file = (self._local_latex_dir / section_path).with_suffix(".tex")
        shared_section_file = (self._paths.shared_latex_dir / section_path).with_suffix(
            ".tex"
        )
        if local_section_file.exists():
            section_file = local_section_file
            relative_path = section_file.relative_to(self._local_dir)
        elif shared_section_file.exists():
            section_file = shared_section_file
            relative_path = section_file.relative_to(self._paths.shared_dir)
        else:
            return None
        config_file = section_file.with_suffix(".yml")
        items: List[ContentOrTitle] = []
        if "title" in config:
            title = config["title"]
        elif config_file.exists():
            title = yaml_safe_load(config_file.read_text(encoding="utf8"))["title"]
        else:
            title = None
        if title is not None:
            items.append(Title(title=title, level=title_level))
        items.append(str(relative_path))
        dependencies = Dependencies()
        dependencies.used.add(section_file.resolve())
        return items, dependencies


@attrs(auto_attribs=True)
class Targets(Iterable[Target]):
    _targets: List[Target] = attrib()
    _paths: Paths

    @classmethod
    def from_file(
        cls, paths: Paths, whitelist: Optional[List[str]] = None
    ) -> "Targets":
        if not paths.targets.exists():
            raise DeckzException(f"Could not find {paths.targets}.")
        content = yaml_safe_load(paths.targets.read_text(encoding="utf8"))
        return cls.from_data(data=content, paths=paths, whitelist=whitelist)

    @classmethod
    def from_data(
        cls,
        data: List[Dict[str, Any]],
        paths: Paths,
        whitelist: Optional[List[str]] = None,
    ) -> "Targets":
        for target_data in data:
            if target_data["name"] == "all":
                raise DeckzException(
                    f"Incorrect targets {paths.targets}: "
                    '"all" is a reserved target name.'
                )
        targets = [TargetBuilder(data=target, paths=paths).build() for target in data]
        if whitelist is not None:
            cls._filter_targets(targets, frozenset(whitelist), paths)
        return Targets(targets, paths)

    @_targets.validator
    def _check_targets(self, attribute: Attribute, value: List[Target]) -> None:
        missing_dependencies = {
            target.name: target.dependencies.missing
            for target in value
            if target.dependencies.missing
        }
        if missing_dependencies:
            raise DeckzException(
                f"Incorrect targets {self._paths.targets}. "
                "Could not find the following dependencies:\n%s"
                % "\n".join(
                    "  - %s:\n%s" % (k, "\n".join(f"    - {p}" for p in v))
                    for k, v in missing_dependencies.items()
                ),
            )

    @staticmethod
    def _filter_targets(
        targets: Iterable[Target], whiteset: FrozenSet[str], paths: Paths
    ) -> List[Target]:
        unmatched = whiteset - frozenset(target.name for target in targets)
        if unmatched:
            raise DeckzException(
                f"Incorrect targets {paths.targets}. "
                "Could not find the following targets:\n%s"
                % "\n".join("  - %s" % name for name in unmatched)
            )
        return [target for target in targets if target.name in whiteset]

    def __iter__(self) -> Iterator[Target]:
        return iter(self._targets)

    def __len__(self) -> int:
        return len(self._targets)
