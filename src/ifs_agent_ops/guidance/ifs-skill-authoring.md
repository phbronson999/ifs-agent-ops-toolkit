# IFS Skill Authoring Guide

How to build, format, and save IFS resource guide (skill) files.

Use the `call_protected_api` tool with the endpoints below to interact with IFS Cloud.

---

## Skill file format

Every skill must follow this structure exactly:

```markdown
# [Guide Name]

[1–2 sentence description of what this guide enables.]

Use the `call_protected_api` tool with the endpoints below to [purpose].

## Base Path

All endpoints below are relative to this base:
```
/main/ifsapplications/projection/v1/[ServiceName].svc
```

---

## 1. [Operation Name]

[What this operation does in plain language.]

**Endpoint:**
```
/main/ifsapplications/projection/v1/[ServiceName].svc/[EntitySet]
```

**Method:** GET | POST | PATCH | DELETE

**Useful fields for `$select`:**
`Field1`, `Field2`, `Field3`

**Examples:**

[Business-meaningful label]:
```
[example URL or request body]
```

**Response shape:**
```json
{ "value": [{ ... }] }
```

[Repeat for each operation]

---

## Recommended Workflows

[Numbered multi-step workflows combining calls]

> Before fetching large datasets, add `$count=true` to check result size. If results exceed 10 records, confirm with the user before fetching all data.

For OData query syntax, use `get_api_guide({ guide: "ifs-common-odata-reference" })`.
```

---

## Saving the skill

1. Ask the user: "What should the filename be? Use the format `ifs-[module]-[area]`, e.g. `ifs-sales-customers` — saved as .md". If updating an existing skill, use its current filename.
2. Call `save_skill({ filename: "...", content: "..." })` to save it.
3. Check the result:
   - `action: "created"` — confirm the skill is saved and available via `get_api_guide`. No restart needed.
   - `action: "updated"` — show the `changes` summary so the user can see what changed.
   - Error — report it and ask the user to check the filename or path.
4. After saving, others can import the skill with `import_skill({ source: "HTTPS URL to the .md file" })`.

---

## HAR mode guidelines

When building from a HAR file:

- Work through one projection service at a time
- For each operation (entity set + method), ask the user what it represents in their workflow
- Replace all real data values with realistic placeholders (e.g. `CUST-001`, `PO-10001`, `2024-01-15`)
- Preserve IfsApp enum values exactly as seen — they are structural, not sensitive
- Only include operations and filter patterns the user confirms are useful
- Ask at most 2 questions at a time and use the user's own words in descriptions

---

## Debugging ODP_DESERIALIZATION_ERROR

When a POST to an IFS action returns `ODP_DESERIALIZATION_ERROR`, a field in the request body has the wrong JSON type. IFS action parameters are often typed differently from what you'd expect — for example, a boolean-looking field like `RentalDb` may be `Edm.String` and require `"FALSE"` not `false`.

**Step 1 — Check the OpenAPI spec for the action's parameter types:**

```
call_protected_api GET /main/ifsapplications/projection/v1/{ProjectionName}.svc/$openapi?V2
```

The spec is large (~4MB) and will be saved to a local temp file rather than shown inline. Use Python to extract the definition you need:

```python
import json

with open(r'<path to saved file>', 'r', encoding='utf-8') as f:
    raw = json.load(f)['result']
spec = json.loads(raw)['data']

# Print action parameter types
print(json.dumps(spec['definitions']['YourActionNameActionImport'], indent=2))
```

Replace `YourActionNameActionImport` with the action name + `ActionImport` (e.g. `ValidateAndFetchCatalogNoDefaultsActionImport`).

**Step 2 — Apply the type rules:**

| Spec type | Send as |
|-----------|---------|
| `"type": "string"` | JSON string — even if it looks like a boolean: `"FALSE"`, `"TRUE"` |
| `"type": "number"` | JSON number: `0`, `1500`, `2.66` |
| `"type": "string"` + date example `"2019-10-10"` | Date-only string: `"2026-05-17"` — never a datetime offset |
| `null` allowed | `null` is safe to pass for optional fields |

**Common traps across IFS actions:**

IFS action parameters frequently use `Edm.String` for fields that look like other types. This is a widespread pattern — not limited to any single projection. Always verify against the spec rather than assuming.

| Pattern | Wrong | Correct |
|---------|-------|---------|
| Boolean-looking flags (`RentalDb`, `FreeOfChargeDb`, etc.) | `false` | `"FALSE"` |
| Date parameters | `"2026-05-17T00:00:00Z"` | `"2026-05-17"` |
| Numeric identity/key fields (`Identity4`, sequence numbers) | `"0"` (string) | `0` (number) — verify in spec |
| Enum-like string codes | `true` / `1` | `"Y"` or `"Yes"` — depends on spec |

**Alternative: extract the body from a HAR recording**

If the spec lookup is inconvenient, record a HAR of the browser making the same call and extract the body with Python:

```python
import json
with open(r'path/to/file.har', 'r', encoding='utf-8') as f:
    har = json.load(f)
for entry in har['log']['entries']:
    req = entry['request']
    if 'ActionName' in req['url']:
        print(req.get('postData', {}).get('text', ''))
```

Note: the browser sends all numbers as strings due to `IEEE754Compatible=true`. The spec is the authoritative source for the actual types.

### DateTime fields in write actions

IFS write actions (POST/PATCH) that accept date/time values require **local server time without any timezone suffix** — e.g. `"2026-05-18T10:00:00"`. Do **not** append `Z` or any UTC offset (`+00:00`, `-05:00`, etc.).

IFS compares the submitted value against the database server's local clock directly. Sending a `Z`-suffixed string causes IFS to treat the value as if it were local time but shifted by the UTC offset, making it appear to be in the future — this triggers errors like `WRNGSTARTTIME: Start Time for a clocking cannot be a future time`.

Query responses return timestamps in UTC with a `Z` suffix — the no-suffix rule applies **only to write request bodies**.

When documenting time fields in a skill, use this note:

> `StartTime` / `StopTime` / `FinishTime` must be local server time with no timezone suffix: `"2026-05-18T10:00:00"`. Do not use `Z` or UTC offsets in POST bodies.

---

## OpenAPI mode guidelines

When building from an OpenAPI/Swagger spec (downloaded file or live fetch):

- Not everything in the spec needs to go into the skill — work selectively
- For each relevant entity set, ask the user what it represents in their business
- Derive realistic placeholder values matching field type and maxLength
- Suggest common `$filter` patterns: `contains(Name,'{value}')` for strings, `eq '{value}'` for key fields
- For POST/PATCH: document required fields clearly, list optional fields in a reference table
- Ask about multi-step workflows (e.g. create → validate → link to another entity)
- For enums: list all values with a short explanation (ask the user what each means)
- If unsure whether a field is an enum or need its fully qualified type for $filter, call `get_projection_enum_map` with both `projection_name` and `entity_set` (e.g. `entity_set: "CustomerOrderSet"`) — this returns only that entity set's enums and keeps the response small. Only omit `entity_set` if you need to discover all available entity sets in the projection.
- Ask at most 2 questions at a time and use the user's own words in descriptions
