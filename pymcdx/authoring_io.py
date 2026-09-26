"""Safe YAML loading for semantic worksheet authoring documents."""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, cast

import yaml  # type: ignore[reportMissingTypeStubs]
from pydantic import ValidationError

from pymcdx.authoring_models import AuthoringDocument, ImageBlock


class AuthoringDocumentError(ValueError):
    """Stable, path-bearing failure raised at the authoring document boundary."""

    source_name: str
    path: str
    code: str

    def __init__(
        self,
        message: str,
        *,
        source_name: str,
        path: str,
        code: str,
    ) -> None:
        self.source_name = source_name
        self.path = path
        self.code = code
        super().__init__(f"{source_name}:{path}: {code}: {message}")


class _YamlLoader(Protocol):
    def construct_object(self, node: object, deep: bool = False) -> object: ...


class _YamlMappingNode(Protocol):
    value: list[tuple[object, object]]


class _DuplicateKeyError(yaml.YAMLError):  # type: ignore[misc]
    """Internal parser error retaining the duplicate key's source mark."""

    key: str
    line: int
    column: int

    def __init__(self, key: str, *, line: int, column: int) -> None:
        self.key = key
        self.line = line
        self.column = column
        super().__init__(f"duplicate mapping key {key!r}")


class _StrictSafeLoader(yaml.SafeLoader):  # type: ignore[misc]
    """SafeLoader variant that rejects duplicate mapping keys."""


def _construct_mapping(
    loader: object,
    node: object,
    deep: bool = False,
) -> dict[str, object]:
    typed_loader = cast(_YamlLoader, loader)
    typed_node = cast(_YamlMappingNode, node)
    mapping: dict[str, object] = {}
    for key_node, value_node in typed_node.value:
        key = typed_loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            raise ValueError("mapping keys must be strings")
        if key in mapping:
            mark = getattr(key_node, "start_mark", None)
            line = getattr(mark, "line", 0) + 1
            column = getattr(mark, "column", 0) + 1
            raise _DuplicateKeyError(key, line=line, column=column)
        mapping[key] = typed_loader.construct_object(value_node, deep=deep)
    return mapping


_StrictSafeLoader.add_constructor(  # type: ignore[reportUnknownMemberType]
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping,
)


def _path_for_location(location: object) -> str:
    """Format a Pydantic location into a deterministic document path."""

    if not isinstance(location, tuple):
        return "$"
    path = "$"
    for part in location:
        if isinstance(part, int):
            path += f"[{part}]"
        elif isinstance(part, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", part):
            path += f".{part}"
        else:
            path += f"[{part!r}]"
    return path


def _embedded_path(message: str) -> str | None:
    """Extract a path included by a model-level reference invariant."""

    match = re.search(
        r"(?P<path>blocks\[\d+\](?:\.style|\.runs\[\d+\]\.style)):",
        message,
    )
    return f"$.{match.group('path')}" if match else None


def _validation_error(source_name: str, error: ValidationError) -> AuthoringDocumentError:
    details = error.errors()
    if not details:
        return AuthoringDocumentError(
            "document failed validation",
            source_name=source_name,
            path="$",
            code="validation_error",
        )
    first = details[0]
    message = str(first.get("msg", "document failed validation"))
    path = _embedded_path(message) or _path_for_location(first.get("loc"))
    return AuthoringDocumentError(
        message,
        source_name=source_name,
        path=path,
        code="validation_error",
    )


def _yaml_error(source_name: str, error: yaml.YAMLError) -> AuthoringDocumentError:  # type: ignore[name-defined]
    if isinstance(error, _DuplicateKeyError):
        return AuthoringDocumentError(
            f"duplicate mapping key {error.key!r} at line {error.line}, column {error.column}",
            source_name=source_name,
            path="$",
            code="yaml_duplicate_key",
        )

    mark = getattr(error, "problem_mark", None)
    line = getattr(mark, "line", None)
    column = getattr(mark, "column", None)
    location = "$"
    if isinstance(line, int) and isinstance(column, int):
        location = f"$ (line {line + 1}, column {column + 1})"
    detail = str(error).splitlines()[0] if str(error) else "invalid YAML"
    return AuthoringDocumentError(
        detail,
        source_name=source_name,
        path=location,
        code="yaml_parse_error",
    )


def parse_document(
    source: str,
    *,
    source_name: str = "<string>",
    transform: Callable[[dict[str, object]], dict[str, object]] | None = None,
) -> AuthoringDocument:
    """Parse and validate one semantic YAML document without code execution.

    Duplicate mapping keys are rejected before Pydantic validation, including
    nested mappings.  All failures carry a stable source name, path, and code.
    """

    if not source.strip():
        raise AuthoringDocumentError(
            "document is empty",
            source_name=source_name,
            path="$",
            code="empty_document",
        )
    if source.startswith("\ufeff"):
        raise AuthoringDocumentError(
            "UTF-8 BOM is not accepted; decode the source before parsing",
            source_name=source_name,
            path="$",
            code="bom_not_allowed",
        )

    try:
        loaded: object = yaml.load(  # type: ignore[reportUnknownMemberType]
            source,
            Loader=_StrictSafeLoader,  # noqa: S506 - subclass of SafeLoader rejects constructors
        )
    except _DuplicateKeyError as error:
        raise _yaml_error(source_name, error) from error
    except yaml.YAMLError as error:  # type: ignore[name-defined]
        raise _yaml_error(source_name, error) from error
    except ValueError as error:
        raise AuthoringDocumentError(
            str(error),
            source_name=source_name,
            path="$",
            code="yaml_mapping_error",
        ) from error

    if loaded is None:
        raise AuthoringDocumentError(
            "document must contain a mapping",
            source_name=source_name,
            path="$",
            code="root_not_mapping",
        )
    if not isinstance(loaded, dict):
        raise AuthoringDocumentError(
            f"document root must be a mapping, got {type(loaded).__name__}",
            source_name=source_name,
            path="$",
            code="root_not_mapping",
        )

    try:
        if transform is not None:
            loaded = transform(loaded)
        return AuthoringDocument.model_validate(loaded)
    except ValidationError as error:
        raise _validation_error(source_name, error) from error


def load_document(
    path: Path,
    *,
    transform: Callable[[dict[str, object]], dict[str, object]] | None = None,
) -> AuthoringDocument:
    """Read UTF-8 YAML from *path* and validate it as an authoring document."""

    source_name = str(path)
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise AuthoringDocumentError(
            str(error),
            source_name=source_name,
            path="$",
            code="read_error",
        ) from error
    document = parse_document(source, source_name=source_name, transform=transform)
    return resolve_image_paths(document, path.parent)


def resolve_image_paths(document: AuthoringDocument, base: Path) -> AuthoringDocument:
    """Make relative image paths relative to the YAML file's folder."""

    blocks = [
        block.model_copy(update={"path": str(base / block.path)})
        if isinstance(block, ImageBlock) and not Path(block.path).is_absolute()
        else block
        for block in document.blocks
    ]
    return document.model_copy(update={"blocks": blocks})


__all__ = ["AuthoringDocumentError", "load_document", "parse_document", "resolve_image_paths"]
