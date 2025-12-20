import argparse
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict, deque
from typing import Dict, List, Optional, Set, Tuple

PARENT_KEYS = {
    "parent", "parentid", "parent_id", "parentguid", "parent_guid",
    "parentuid", "parent_uuid", "parentref", "parent_ref", "parentname", "parent_name",
    "parentnode", "parent_node", "parentobject", "parent_object",
    # common PascalCase variants
    "parentid".lower(), "parentguid".lower(), "parentuid".lower(),
}

CHILD_KEYS = {
    "children", "child", "childrenids", "children_ids", "childids", "child_ids",
    "kid", "kids", "descendant", "descendants",
    # common PascalCase variants normalized to lowercase
}

TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


def list_xml_files(folder: str, max_files: Optional[int] = None) -> List[str]:
    files = []
    for name in os.listdir(folder):
        if name.lower().endswith(".xml"):
            files.append(os.path.join(folder, name))
    files.sort()
    if max_files is not None:
        files = files[:max_files]
    return files


def discover_ids(xml_paths: List[str]) -> Set[str]:
    ids = set()
    for p in xml_paths:
        base = os.path.splitext(os.path.basename(p))[0]
        ids.add(base)
    return ids


def normalize_id(value: Optional[str], known_ids: Set[str]) -> Optional[str]:
    if not value:
        return None
    v = value.strip()
    if v in known_ids:
        return v
    # Handle references like ".../ABC123.xml" or "ABC123.xml"
    if v.lower().endswith(".xml"):
        base = os.path.splitext(os.path.basename(v))[0]
        if base in known_ids:
            return base
    # Try to find any token within the string that matches a known ID
    for tok in TOKEN_RE.findall(v):
        if tok in known_ids:
            return tok
    return None


def extract_parent_and_children(tree: ET.ElementTree, known_ids: Set[str]) -> Tuple[Optional[str], Set[str]]:
    parent: Optional[str] = None
    children: Set[str] = set()
    root = tree.getroot()

    for elem in root.iter():
        # Attributes that indicate parent/child relationships
        for k, v in elem.attrib.items():
            lk = k.lower()
            if lk in PARENT_KEYS and parent is None:
                pid = normalize_id(v, known_ids)
                if pid:
                    parent = pid
            elif lk in CHILD_KEYS:
                # Attributes may include lists
                for tok in TOKEN_RE.findall(v):
                    cid = normalize_id(tok, known_ids)
                    if cid:
                        children.add(cid)
        # Element text indicating parent
        tag_l = elem.tag.lower()
        if tag_l in PARENT_KEYS and parent is None:
            pid = normalize_id(elem.text or "", known_ids)
            if pid:
                parent = pid
        # Element text indicating children
        if tag_l in CHILD_KEYS:
            txt = elem.text or ""
            for tok in TOKEN_RE.findall(txt):
                cid = normalize_id(tok, known_ids)
                if cid:
                    children.add(cid)
    return parent, children


def build_graph(objects_folder: str, max_files: Optional[int] = None, verbose: bool = False):
    xml_files = list_xml_files(objects_folder, max_files)
    known_ids = discover_ids(xml_files)
    if verbose:
        print(f"Found {len(xml_files)} XML files and {len(known_ids)} IDs")

    parent_of: Dict[str, Optional[str]] = {os.path.splitext(os.path.basename(p))[0]: None for p in xml_files}
    children_of: Dict[str, Set[str]] = defaultdict(set)

    for path in xml_files:
        obj_id = os.path.splitext(os.path.basename(path))[0]
        try:
            tree = ET.parse(path)
        except ET.ParseError as e:
            if verbose:
                print(f"WARN: Failed to parse {path}: {e}")
            continue
        parent, children = extract_parent_and_children(tree, known_ids)
        if parent:
            parent_of[obj_id] = parent
            children_of[parent].add(obj_id)
        for c in children:
            # Only add child edge if not contradicting known parent
            children_of[obj_id].add(c)
            if parent_of.get(c) is None:
                # Do not override if already set by an explicit parent tag
                parent_of[c] = obj_id
        if verbose and (parent or children):
            print(f"{obj_id}: parent={parent} children={sorted(children)}")

    # Ensure every node has an entry in children_of
    for nid in known_ids:
        children_of.setdefault(nid, set())

    return known_ids, parent_of, children_of


def compute_roots(known_ids: Set[str], parent_of: Dict[str, Optional[str]]) -> Set[str]:
    roots = set()
    for nid in known_ids:
        if not parent_of.get(nid):
            roots.add(nid)
    return roots


def compute_heights(children_of: Dict[str, Set[str]]) -> Dict[str, int]:
    sys.setrecursionlimit(max(10000, sys.getrecursionlimit()))
    memo: Dict[str, int] = {}
    visiting: Set[str] = set()

    def height(node: str) -> int:
        if node in memo:
            return memo[node]
        if node in visiting:
            # Cycle detected; treat as 0 to break
            return 0
        visiting.add(node)
        h = 0
        for c in children_of.get(node, ()): 
            h = max(h, 1 + height(c))
        visiting.remove(node)
        memo[node] = h
        return h

    # Compute for all nodes
    for node in children_of.keys():
        height(node)
    return memo


def to_json_graph(known_ids: Set[str], parent_of: Dict[str, Optional[str]], children_of: Dict[str, Set[str]]):
    nodes = [{"id": nid, "parent": parent_of.get(nid)} for nid in sorted(known_ids)]
    edges = []
    for p, chs in children_of.items():
        for c in chs:
            edges.append({"parent": p, "child": c})
    return {"nodes": nodes, "edges": edges}


def main():
    parser = argparse.ArgumentParser(description="Analyze XML objects to build parent/children graph and compute highest roots.")
    parser.add_argument("objects_folder", nargs="?", default=os.path.join("Pacovip", "objects"), help="Path to the objects XML folder")
    parser.add_argument("--max-files", type=int, default=None, help="Limit the number of XML files scanned")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose parsing logs")
    parser.add_argument("--json-out", type=str, default=None, help="Optional path to write the graph as JSON")
    parser.add_argument("--top", type=int, default=None, help="Show only the top N highest roots by height")

    args = parser.parse_args()

    folder = args.objects_folder
    if not os.path.isdir(folder):
        print(f"ERROR: Folder not found: {folder}")
        sys.exit(1)

    known_ids, parent_of, children_of = build_graph(folder, max_files=args.max_files, verbose=args.verbose)
    roots = compute_roots(known_ids, parent_of)
    heights = compute_heights(children_of)

    # Highest roots: sort roots by height desc, then by id
    sorted_roots = sorted(list(roots), key=lambda r: (-heights.get(r, 0), r))
    if args.top:
        sorted_roots = sorted_roots[:args.top]

    print("Highest roots (by height):")
    for r in sorted_roots:
        print(f"- {r} (height={heights.get(r, 0)})")

    if args.json_out:
        graph_json = to_json_graph(known_ids, parent_of, children_of)
        try:
            with open(args.json_out, "w", encoding="utf-8") as f:
                json.dump(graph_json, f, indent=2)
            print(f"Graph JSON written to {args.json_out}")
        except OSError as e:
            print(f"ERROR: Failed to write JSON: {e}")


if __name__ == "__main__":
    main()
