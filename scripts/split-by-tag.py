#!/usr/bin/env python3
"""
Split xero_accounting.yaml into tag-based OpenAPI specs.

Usage:
    python scripts/split-by-tag.py

Generates five files at the repo root:
    xero_accounting_settings.yaml
    xero_accounting_transactions.yaml
    xero_accounting_reports.yaml
    xero_accounting_contacts.yaml
    xero_accounting_attachments.yaml

Each output:
  - Contains only operations tagged with the target tag
  - Includes transitive closure of all referenced OpenAPI components
  - Preserves the shared 'Accounting' tag entry alongside the target tag
  - Has all x-* vendor extension fields stripped at every depth

Prerequisites: PyYAML  (pip install pyyaml)
"""

import os
import sys
from copy import deepcopy

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML is required. Install it with: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
SOURCE = os.path.join(REPO_ROOT, "xero_accounting.yaml")

TAG_MAP = {
    "Settings":     "xero_accounting_settings.yaml",
    "Transactions": "xero_accounting_transactions.yaml",
    "Reports":      "xero_accounting_reports.yaml",
    "Contacts":     "xero_accounting_contacts.yaml",
    "Attachments":  "xero_accounting_attachments.yaml",
}

HTTP_METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def strip_x_fields(obj):
    """Recursively remove every dict key that starts with 'x-'."""
    if isinstance(obj, dict):
        return {k: strip_x_fields(v) for k, v in obj.items() if not k.startswith("x-")}
    if isinstance(obj, list):
        return [strip_x_fields(item) for item in obj]
    return obj


def collect_refs(obj, refs=None):
    """Walk an arbitrary object and collect every local $ref string."""
    if refs is None:
        refs = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "$ref" and isinstance(v, str) and v.startswith("#/"):
                refs.add(v)
            else:
                collect_refs(v, refs)
    elif isinstance(obj, list):
        for item in obj:
            collect_refs(item, refs)
    return refs


def resolve_ref(spec, ref):
    """Resolve a local $ref like '#/components/schemas/Foo' to its node."""
    parts = ref.lstrip("#/").split("/")
    node = spec
    for part in parts:
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def transitive_closure(spec, initial_refs):
    """
    Return the full set of local $refs reachable from initial_refs.
    Uses a visited set so circular $ref graphs terminate.
    """
    visited = set()
    queue = list(initial_refs)
    while queue:
        ref = queue.pop()
        if ref in visited:
            continue
        visited.add(ref)
        target = resolve_ref(spec, ref)
        if target is not None:
            for r in collect_refs(target):
                if r not in visited:
                    queue.append(r)
    return visited


def set_nested(container, parts, value):
    """Set container[parts[0]][parts[1]]... = value, creating dicts as needed."""
    for part in parts[:-1]:
        container = container.setdefault(part, {})
    container[parts[-1]] = value


def count_operations(paths):
    return sum(
        1
        for path_item in paths.values()
        for method in HTTP_METHODS
        if method in path_item
    )


# ---------------------------------------------------------------------------
# Core extraction
# ---------------------------------------------------------------------------

def extract_spec(spec, tag):
    """
    Build a minimal valid OpenAPI document containing only operations tagged
    with `tag` and every component they transitively depend on.
    """

    # -- 1. Filter paths to operations that carry the target tag --------------
    filtered_paths = {}
    for path, path_item in spec.get("paths", {}).items():
        # Path-level fields (parameters, summary, description, servers …)
        # are kept when any operation under this path matches.
        path_level_fields = {k: v for k, v in path_item.items() if k not in HTTP_METHODS}
        matched_ops = {
            method: path_item[method]
            for method in HTTP_METHODS
            if method in path_item and tag in path_item[method].get("tags", [])
        }
        if matched_ops:
            filtered_paths[path] = {**path_level_fields, **matched_ops}

    if not filtered_paths:
        print(f"  ERROR: no operations found for tag '{tag}'", file=sys.stderr)
        sys.exit(1)

    # -- 2. Transitive closure of all component $refs -------------------------
    initial_refs = collect_refs(filtered_paths)
    all_refs = transitive_closure(spec, initial_refs)

    # -- 3. Build a components subtree with only the reachable members --------
    components: dict = {}
    for ref in sorted(all_refs):  # sorted for deterministic output order
        parts = ref.lstrip("#/").split("/")
        # Only handle refs of the form #/components/<section>/<name>
        if parts[0] != "components" or len(parts) != 3:
            continue
        value = resolve_ref(spec, ref)
        if value is not None:
            set_nested(components, parts[1:], deepcopy(value))

    # -- 3b. Add securitySchemes with only the scopes used by kept ops --------
    used_scopes = set()
    for path_item in filtered_paths.values():
        for method in HTTP_METHODS:
            op = path_item.get(method)
            if not isinstance(op, dict):
                continue
            for sec_req in op.get("security", []):
                for scheme_name, scope_list in sec_req.items():
                    used_scopes.update(scope_list)

    src_schemes = spec.get("components", {}).get("securitySchemes", {})
    if src_schemes:
        filtered_schemes = {}
        for scheme_name, scheme_def in src_schemes.items():
            scheme_copy = deepcopy(scheme_def)
            # Filter scopes inside every flow down to only those used
            flows = scheme_copy.get("flows", {})
            for flow in flows.values():
                if "scopes" in flow:
                    flow["scopes"] = {
                        s: desc for s, desc in flow["scopes"].items()
                        if s in used_scopes
                    }
            filtered_schemes[scheme_name] = scheme_copy
        components["securitySchemes"] = filtered_schemes

    # -- 4. Build tags list: keep Accounting + target tag ---------------------
    keep_tag_names = {"Accounting", tag}
    filtered_tags = [
        t for t in spec.get("tags", [])
        if isinstance(t, dict) and t.get("name") in keep_tag_names
    ]

    # -- 5. Assemble output document ------------------------------------------
    out: dict = {}
    out["openapi"] = spec.get("openapi", "3.0.0")
    out["info"]    = deepcopy(spec.get("info", {}))
    out["servers"] = deepcopy(spec.get("servers", []))
    if "security" in spec:
        out["security"] = deepcopy(spec["security"])
    out["tags"]  = filtered_tags
    out["paths"] = filtered_paths
    if components:
        out["components"] = components

    # -- 6. Strip all x-* vendor extensions -----------------------------------
    out = strip_x_fields(out)

    return out


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def find_dangling_refs(spec):
    """Return a sorted list of local $refs whose targets do not exist."""
    return sorted(
        ref for ref in collect_refs(spec)
        if resolve_ref(spec, ref) is None
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    # Prerequisites check
    if not os.path.exists(SOURCE):
        print(f"ERROR: source file not found: {SOURCE}", file=sys.stderr)
        sys.exit(1)

    print(f"Loading {SOURCE} ...")
    with open(SOURCE, "r", encoding="utf-8") as fh:
        spec = yaml.safe_load(fh)

    print()
    errors = 0

    for tag, filename in TAG_MAP.items():
        output_path = os.path.join(REPO_ROOT, filename)
        print(f"[{tag}] -> {filename}")

        out = extract_spec(spec, tag)

        # Integrity check
        dangling = find_dangling_refs(out)
        if dangling:
            print(f"  WARNING: {len(dangling)} dangling $ref(s):")
            for d in dangling:
                print(f"    {d}")
            errors += 1

        # Write output
        with open(output_path, "w", encoding="utf-8", newline="\n") as fh:
            yaml.dump(
                out,
                fh,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
                width=120,
            )

        op_count     = count_operations(out.get("paths", {}))
        schema_count = len(out.get("components", {}).get("schemas", {}))
        param_count  = len(out.get("components", {}).get("parameters", {}))
        resp_count   = len(out.get("components", {}).get("responses", {}))
        print(f"  operations={op_count}  schemas={schema_count}  "
              f"parameters={param_count}  responses={resp_count}")

    print()
    if errors:
        print(f"Completed with {errors} warning(s). See output above.", file=sys.stderr)
        sys.exit(1)
    else:
        print("All five specs generated successfully.")


if __name__ == "__main__":
    main()
