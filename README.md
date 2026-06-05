# openapi-lookup

A small CLI for exploring an **OpenAPI 3.0** spec from the terminal. Look up a
single operation and render its parameters, request body, and responses as
**TypeScript interfaces**, or search across the whole spec by path or by
request/response field name.

It resolves `$ref`s on the fly (including nested ones), so you get fully
expanded types without pre-bundling the spec.

## Requirements

- Python 3.10+ (uses `X | Y` type-hint syntax)
- [PyYAML](https://pyyaml.org/)

```bash
pip install -r requirements.txt
```

## Usage

```
python openapi_lookup.py <openapi_file.yaml> <method> <url> [options]
python openapi_lookup.py <openapi_file.yaml> --search [METHOD] <keyword>
python openapi_lookup.py <openapi_file.yaml> --search-response [METHOD] <keyword>
python openapi_lookup.py <openapi_file.yaml> --search-request [METHOD] <keyword>
```

### Lookup mode

Pass a method and a URL path to inspect a single operation. The URL is matched
against the spec's path templates, so concrete values for path parameters work
(e.g. `/users/42` matches `/users/{id}`).

| Option          | Output                                              |
| --------------- | --------------------------------------------------- |
| `--summary`     | The operation summary                               |
| `--description` | The operation description                           |
| `--params`      | Path/query/header/cookie parameters as interfaces   |
| `--request`     | The request body as a `RequestBody` interface       |
| `--response`    | `200`/`201` responses as `Response200` / `Response201` interfaces |

When no option is given, **all** sections are printed (each prefixed with a
`// label` comment so the output stays parseable as TypeScript).

```bash
# Everything about an operation
python openapi_lookup.py api.yaml get /users/{id}

# Just the request and response shapes
python openapi_lookup.py api.yaml post /orders --request --response

# Pipe generated types straight into a file
python openapi_lookup.py api.yaml get /products --params --response > types.ts
```

### Search mode

Search is case-insensitive and treats the keyword as a **regular expression**.
An optional leading HTTP method narrows results.

```bash
# Find paths matching a keyword
python openapi_lookup.py api.yaml --search orders

# Restrict to a single method
python openapi_lookup.py api.yaml --search GET orders

# Find operations whose response body contains a field
python openapi_lookup.py api.yaml --search-response publicToken

# Dotted field paths work too
python openapi_lookup.py api.yaml --search-response GET card.publicToken

# Search request body fields
python openapi_lookup.py api.yaml --search-request GET amount
```

`--search-response` / `--search-request` print each matching endpoint followed
by the matching field paths (prefixed with the response code or `body`).

## Type generation notes

- Scalars map as `string → string`, `integer`/`number → number`,
  `boolean → boolean`, `null → null`.
- `enum` becomes a union of literals; `anyOf`/`oneOf` become `|` unions and
  `allOf` becomes an `&` intersection.
- `array` becomes `Array<...>`; objects with no properties become
  `Record<string, unknown>`.
- Required-ness is honored (`name?:` for optional properties), and
  `description`s are emitted as `/** ... */` doc comments.
- Object schemas become `interface`s; non-object schemas become `type` aliases.
- JSON media types are preferred when an operation declares multiple content
  types.

## Exit codes

Exits non-zero when the file is missing, the YAML fails to parse, no operation
matches, or a search returns no results. A non-3.x `openapi` version prints a
warning but does not abort.
