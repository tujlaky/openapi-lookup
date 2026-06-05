#!/usr/bin/env python3
"""
OpenAPI 3.0 operation lookup tool.

Usage:
    python openapi_lookup.py <openapi_file.yaml> <method> <url> [options]
    python openapi_lookup.py <openapi_file.yaml> --search [METHOD] <keyword>
    python openapi_lookup.py <openapi_file.yaml> --search-response [METHOD] <keyword>
    python openapi_lookup.py <openapi_file.yaml> --search-request [METHOD] <keyword>

Lookup options:
    --summary      Print the operation summary
    --description  Print the operation description
    --params       Print path/query parameters as a TypeScript interface
    --request      Print the request body as a TypeScript interface
    --response     Print 200/201 responses as TypeScript interfaces

When no lookup options are given, all sections are printed.

Examples:
    python openapi_lookup.py api.yaml get /users/{id}
    python openapi_lookup.py api.yaml post /orders --request --response
    python openapi_lookup.py api.yaml get /products --params --response > types.ts
    python openapi_lookup.py api.yaml --search orders
    python openapi_lookup.py api.yaml --search GET orders
    python openapi_lookup.py api.yaml --search-response publicToken
    python openapi_lookup.py api.yaml --search-response GET card.publicToken
    python openapi_lookup.py api.yaml --search-request GET amount
"""

import argparse
import re
import sys

import yaml


# ---------------------------------------------------------------------------
# Spec loading & $ref resolution
# ---------------------------------------------------------------------------

def load_spec(filepath: str) -> dict:
    with open(filepath, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def resolve_ref(spec: dict, ref: str) -> dict | None:
    if not ref.startswith("#/"):
        return None
    parts = ref[2:].split("/")
    node = spec
    for part in parts:
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def deep_resolve(spec: dict, node, _depth: int = 0):
    if _depth > 20:
        return node
    if isinstance(node, dict):
        if "$ref" in node:
            resolved = resolve_ref(spec, node["$ref"])
            if resolved is not None:
                return deep_resolve(spec, resolved, _depth + 1)
        return {k: deep_resolve(spec, v, _depth + 1) for k, v in node.items()}
    if isinstance(node, list):
        return [deep_resolve(spec, item, _depth + 1) for item in node]
    return node


# ---------------------------------------------------------------------------
# Path matching
# ---------------------------------------------------------------------------

def _path_to_pattern(path_template: str) -> re.Pattern:
    escaped = re.escape(path_template)
    pattern = re.sub(r"\\\{[^}]+\\\}", r"[^/]+", escaped)
    return re.compile(f"^{pattern}$")


def find_operation(spec: dict, method: str, url: str):
    method = method.lower()
    paths: dict = spec.get("paths", {})

    if url in paths and method in paths[url]:
        return url, paths[url], paths[url][method]

    for path_template, path_item in paths.items():
        if method not in path_item:
            continue
        if _path_to_pattern(path_template).match(url):
            return path_template, path_item, path_item[method]

    return None, None, None


# ---------------------------------------------------------------------------
# TypeScript generation
# ---------------------------------------------------------------------------

_TS_SCALAR = {
    "string": "string",
    "integer": "number",
    "number": "number",
    "boolean": "boolean",
    "null": "null",
}


def _schema_to_ts(schema: dict, indent: int = 0) -> str:
    pad = "  " * indent
    inner = "  " * (indent + 1)

    for key in ("anyOf", "oneOf"):
        if key in schema:
            return " | ".join(_schema_to_ts(s, indent) for s in schema[key])
    if "allOf" in schema:
        return " & ".join(_schema_to_ts(s, indent) for s in schema["allOf"])

    enum = schema.get("enum")
    if enum is not None:
        return " | ".join(f'"{v}"' if isinstance(v, str) else str(v) for v in enum)

    t = schema.get("type")

    if t == "array":
        return f"Array<{_schema_to_ts(schema.get('items', {}), indent)}>"

    if t == "object" or "properties" in schema:
        props = schema.get("properties", {})
        required = set(schema.get("required", []))
        additional = schema.get("additionalProperties")

        if not props and additional is None:
            return "Record<string, unknown>"

        lines = ["{"]
        for name, prop in props.items():
            opt = "" if name in required else "?"
            desc = prop.get("description", "")
            ts = _schema_to_ts(prop, indent + 1)
            if desc:
                lines.append(f"{inner}/** {desc} */")
            lines.append(f"{inner}{name}{opt}: {ts};")
        if additional and isinstance(additional, dict):
            lines.append(f"{inner}[key: string]: {_schema_to_ts(additional, indent + 1)};")
        lines.append(f"{pad}}}")
        return "\n".join(lines)

    return _TS_SCALAR.get(t, "unknown")


def _named_interface(spec: dict, schema_node: dict, name: str) -> str:
    ts = _schema_to_ts(deep_resolve(spec, schema_node))
    if ts.startswith("{"):
        return f"interface {name} {ts}"
    return f"type {name} = {ts};"


def _pick_media_obj(content: dict) -> dict | None:
    for mt, obj in content.items():
        if "json" in mt:
            return obj
    return next(iter(content.values()), None) if content else None


# ---------------------------------------------------------------------------
# Section builders — each returns a string or None
# ---------------------------------------------------------------------------

def section_summary(operation: dict, path_item: dict) -> str | None:
    value = operation.get("summary") or path_item.get("summary", "")
    return value.strip() if value.strip() else None


def section_description(operation: dict, path_item: dict) -> str | None:
    value = operation.get("description") or path_item.get("description", "")
    return value.strip() if value.strip() else None


def section_params(spec: dict, operation: dict, path_item: dict) -> str | None:
    raw = list(path_item.get("parameters", [])) + list(operation.get("parameters", []))
    if not raw:
        return None

    # De-duplicate, resolve refs
    seen: set = set()
    params: list[dict] = []
    for p in raw:
        p = deep_resolve(spec, p)
        key = (p.get("name"), p.get("in"))
        if key not in seen:
            seen.add(key)
            params.append(p)

    def _param_ts(p: dict) -> str:
        schema = deep_resolve(spec, p.get("schema", {}))
        return _schema_to_ts(schema, indent=1)

    # Group by location
    groups: dict[str, list[dict]] = {}
    for p in params:
        loc = p.get("in", "other")
        groups.setdefault(loc, []).append(p)

    # Emit one interface per group that has params
    blocks = []
    order = ["path", "query", "header", "cookie"]
    for loc in order + [l for l in groups if l not in order]:
        if loc not in groups:
            continue
        name = loc.capitalize() + "Params"
        lines = [f"interface {name} {{"]
        for p in groups[loc]:
            pname = p.get("name", "unknown")
            opt = "" if p.get("required") else "?"
            desc = p.get("description", "")
            ts = _param_ts(p)
            if desc:
                lines.append(f"  /** {desc} */")
            lines.append(f"  {pname}{opt}: {ts};")
        lines.append("}")
        blocks.append("\n".join(lines))

    return "\n\n".join(blocks) if blocks else None


def section_request(spec: dict, operation: dict) -> str | None:
    rb = operation.get("requestBody")
    if not rb:
        return None
    rb = deep_resolve(spec, rb)
    media_obj = _pick_media_obj(rb.get("content", {}))
    if not media_obj or "schema" not in media_obj:
        return None
    return _named_interface(spec, media_obj["schema"], "RequestBody")


def section_response(spec: dict, operation: dict) -> str | None:
    responses = {str(k): v for k, v in operation.get("responses", {}).items()}
    blocks = []
    for code in ("200", "201"):
        if code not in responses:
            continue
        response = deep_resolve(spec, responses[code])
        media_obj = _pick_media_obj(response.get("content", {}))
        if media_obj and "schema" in media_obj:
            blocks.append(_named_interface(spec, media_obj["schema"], f"Response{code}"))
        else:
            blocks.append(f"type Response{code} = void;")
    return "\n\n".join(blocks) if blocks else None


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

_METHODS = {"get", "post", "put", "delete", "patch", "head", "options", "trace"}


def cmd_search(spec: dict, terms: list[str]) -> None:
    if len(terms) == 2 and terms[0].lower() in _METHODS:
        filter_method, pattern = terms[0].lower(), terms[1]
    elif len(terms) == 1:
        filter_method, pattern = None, terms[0]
    else:
        print("--search expects [METHOD] <pattern>", file=sys.stderr)
        sys.exit(1)

    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        print(f"Invalid regex pattern '{pattern}': {exc}", file=sys.stderr)
        sys.exit(1)

    paths: dict = spec.get("paths", {})
    results = []
    for path_template, path_item in paths.items():
        if not regex.search(path_template):
            continue
        for method, value in path_item.items():
            if method not in _METHODS:
                continue
            if filter_method and method != filter_method:
                continue
            results.append(f"{method.upper()} {path_template}")

    if not results:
        print("No matches found.", file=sys.stderr)
        sys.exit(1)

    print("\n".join(results))


# ---------------------------------------------------------------------------
# Schema field-path collection (for body search)
# ---------------------------------------------------------------------------

def _collect_field_paths(schema: dict, prefix: str = "") -> list[str]:
    """Recursively yield every dotted field path present in a JSON schema."""
    paths = []

    for combiner in ("anyOf", "oneOf", "allOf"):
        for sub in schema.get(combiner, []):
            paths.extend(_collect_field_paths(sub, prefix))

    props = schema.get("properties", {})
    for name, prop in props.items():
        full_path = f"{prefix}.{name}" if prefix else name
        paths.append(full_path)
        paths.extend(_collect_field_paths(prop, full_path))

    if schema.get("type") == "array":
        paths.extend(_collect_field_paths(schema.get("items", {}), prefix))

    return paths


def _search_body(spec: dict, terms: list[str], source: str) -> None:
    """Search request or response body field paths across all operations.

    source: "response" | "request"
    """
    if len(terms) == 2 and terms[0].lower() in _METHODS:
        filter_method, pattern = terms[0].lower(), terms[1]
    elif len(terms) == 1:
        filter_method, pattern = None, terms[0]
    else:
        print(f"--search-{source} expects [METHOD] <pattern>", file=sys.stderr)
        sys.exit(1)

    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        print(f"Invalid regex pattern '{pattern}': {exc}", file=sys.stderr)
        sys.exit(1)

    def _schema_from_response(operation: dict) -> list[tuple[str, dict]]:
        responses = {str(k): v for k, v in operation.get("responses", {}).items()}
        out = []
        for code in ("200", "201"):
            if code not in responses:
                continue
            response = deep_resolve(spec, responses[code])
            media_obj = _pick_media_obj(response.get("content", {}))
            if media_obj and "schema" in media_obj:
                out.append((code, deep_resolve(spec, media_obj["schema"])))
        return out

    def _schema_from_request(operation: dict) -> list[tuple[str, dict]]:
        rb = operation.get("requestBody")
        if not rb:
            return []
        rb = deep_resolve(spec, rb)
        media_obj = _pick_media_obj(rb.get("content", {}))
        if not media_obj or "schema" not in media_obj:
            return []
        return [("body", deep_resolve(spec, media_obj["schema"]))]

    get_schemas = _schema_from_response if source == "response" else _schema_from_request

    paths: dict = spec.get("paths", {})
    results = []
    for path_template, path_item in paths.items():
        for method, operation in path_item.items():
            if method not in _METHODS:
                continue
            if filter_method and method != filter_method:
                continue

            matched = []
            for label, schema in get_schemas(operation):
                for field_path in _collect_field_paths(schema):
                    if regex.search(field_path):
                        matched.append(f"{label}:{field_path}")

            if matched:
                results.append((f"{method.upper()} {path_template}", matched))

    if not results:
        print("No matches found.", file=sys.stderr)
        sys.exit(1)

    for endpoint, fields in results:
        print(endpoint)
        for field in fields:
            print(f"  {field}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Look up an OpenAPI 3.0 operation and output TypeScript interfaces.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("openapi_file", help="Path to the OpenAPI 3.0 YAML file")
    parser.add_argument("method", metavar="METHOD", nargs="?",
                        help="HTTP method (required unless --search is used)")
    parser.add_argument("url", nargs="?",
                        help="URL path to match (required unless --search is used)")
    parser.add_argument("--search", nargs="+", metavar="TERM",
                        help="Search paths: --search <keyword> or --search <METHOD> <keyword>")
    parser.add_argument("--search-response", nargs="+", metavar="TERM",
                        help="Search response body fields: --search-response <keyword> or --search-response <METHOD> <keyword>")
    parser.add_argument("--search-request", nargs="+", metavar="TERM",
                        help="Search request body fields: --search-request <keyword> or --search-request <METHOD> <keyword>")
    parser.add_argument("--summary",     action="store_true", help="Print the operation summary")
    parser.add_argument("--description", action="store_true", help="Print the operation description")
    parser.add_argument("--params",      action="store_true", help="Print parameters as TypeScript interfaces")
    parser.add_argument("--request",     action="store_true", help="Print request body as a TypeScript interface")
    parser.add_argument("--response",    action="store_true", help="Print 200/201 responses as TypeScript interfaces")
    args = parser.parse_args()

    try:
        spec = load_spec(args.openapi_file)
    except FileNotFoundError:
        print(f"Error: file not found: {args.openapi_file}", file=sys.stderr)
        sys.exit(1)
    except yaml.YAMLError as exc:
        print(f"Error parsing YAML: {exc}", file=sys.stderr)
        sys.exit(1)

    if not spec.get("openapi", "").startswith("3."):
        print(f"Warning: expected OpenAPI 3.x, got '{spec.get('openapi', '')}'", file=sys.stderr)

    # --- Search modes ---
    if args.search:
        cmd_search(spec, args.search)
        return

    if args.search_response:
        _search_body(spec, args.search_response, "response")
        return

    if args.search_request:
        _search_body(spec, args.search_request, "request")
        return

    # --- Lookup mode: method and url are required ---
    if not args.method or not args.url:
        parser.error("METHOD and URL are required unless --search is used")

    method = args.method.lower()
    if method not in _METHODS:
        parser.error(f"invalid METHOD '{args.method}'")

    path_template, path_item, operation = find_operation(spec, method, args.url)
    if operation is None:
        print(f"No operation found for {args.method.upper()} {args.url}", file=sys.stderr)
        sys.exit(1)

    show_all = not any([args.summary, args.description, args.params, args.request, args.response])

    sections: list[tuple[str, str | None]] = []
    if show_all or args.summary:
        sections.append(("summary", section_summary(operation, path_item)))
    if show_all or args.description:
        sections.append(("description", section_description(operation, path_item)))
    if show_all or args.params:
        sections.append(("params", section_params(spec, operation, path_item)))
    if show_all or args.request:
        sections.append(("request", section_request(spec, operation)))
    if show_all or args.response:
        sections.append(("response", section_response(spec, operation)))

    visible = [(label, content) for label, content in sections if content]
    multi = len(visible) > 1

    for label, content in visible:
        if multi:
            print(f"// {label}")
        print(content)
        if multi:
            print()


if __name__ == "__main__":
    main()
