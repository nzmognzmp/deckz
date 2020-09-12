from itertools import chain
from logging import getLogger
from pathlib import Path
from subprocess import CalledProcessError, run
from typing import FrozenSet

from yaml import safe_dump, safe_load

from deckz.cli import command
from deckz.exceptions import DeckzException
from deckz.paths import Paths
from deckz.targets import SECTION_YML_VERSION


@command
def migrate_sections() -> None:
    """Find all `section.yml` files and upgrade to newest format."""
    logger = getLogger(__name__)

    new_config_paths = []

    print(get_config_paths())

    for config_path in get_config_paths():
        with config_path.open(encoding="utf8") as fh:
            config = safe_load(fh)
        version = config.get("version", 1)
        if version == SECTION_YML_VERSION:
            continue
        logger.info(
            "Upgrading %s from version %d to %d",
            config_path,
            version,
            SECTION_YML_VERSION,
        )
        if version < 2:
            config_path = _v1_v2(config_path)
        if version < 3:
            config_path = _v2_v3(config_path)
        new_config_paths.append(config_path)

    logger.info("Running prettier on section config files")
    try:
        run(
            ["prettier", "--write", *map(str, new_config_paths)],
            check=True,
            capture_output=True,
        )
    except CalledProcessError as e:
        raise DeckzException(
            "Prettier errored or was not found.\n"
            "Captured stdout\n"
            "---\n"
            "%s\n"
            "Captured stderr\n"
            "---\n%s" % (e.stdout, e.stderr)
        ) from e


def get_config_paths() -> FrozenSet[Path]:
    git_dir = Paths(".", check_depth=False).git_dir
    v1_ymls = git_dir.glob("**/section.yml")
    all_ymls = git_dir.glob("**/*.yml")
    vx_ymls = []
    for yml in all_ymls:
        with yml.open(encoding="utf8") as fh:
            content = safe_load(fh)
            if not isinstance(content, dict):
                continue
            if {"title", "version", "flavors"}.issubset(content):
                vx_ymls.append(yml)
    return frozenset(chain(v1_ymls, vx_ymls))


def _v1_v2(config_path: Path) -> Path:
    with config_path.open(encoding="utf8") as fh:
        config = safe_load(fh)
    title = config["title"]
    includes = [(i if isinstance(i, dict) else {"path": i}) for i in config["includes"]]
    default_titles = {i["path"]: i["title"] for i in includes if "title" in i}
    flavors = {"standard": [i["path"] for i in includes]}
    with config_path.open(encoding="utf8", mode="w") as fh:
        safe_dump(
            dict(
                version=2, title=title, default_titles=default_titles, flavors=flavors
            ),
            fh,
            allow_unicode=True,
        )
    return config_path


def _v2_v3(config_path: Path) -> Path:
    with config_path.open(encoding="utf8") as fh:
        config = safe_load(fh)
    config["version"] = 3
    new_config_path = config_path.parent / f"{config_path.parent.name}.yml"
    with new_config_path.open(encoding="utf8", mode="w") as fh:
        safe_dump(config, fh, allow_unicode=True)
    if config_path != new_config_path:
        config_path.unlink()
    return new_config_path