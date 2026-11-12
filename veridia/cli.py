"""Command line interface. `python -m veridia --help` lists everything."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import ROOT, ConfigError, load_config
from .data import DataError, load_dataset
from .explain import llm_summary, render_text
from .holds import HoldRejected
from .llm import AnthropicClient, parse_free_text
from .requirement import ValidationError
from .service import NotFound, Service

DEFAULT_DATA = ROOT / "data" / "veridia_csv.csv"
DEFAULT_LOG = ROOT / "logs" / "requests.jsonl"


def _requirement_arg(args) -> dict:
    if getattr(args, "example", None):
        path = ROOT / "examples" / f"{args.example}.json"
        if not path.exists():
            names = sorted(p.stem for p in (ROOT / "examples").glob("*.json"))
            raise SystemExit(f"unknown example {args.example!r}; available: {', '.join(names)}")
    elif getattr(args, "requirement", None):
        path = Path(args.requirement)
    else:
        raise SystemExit("give --example NAME or --requirement FILE.json")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"cannot read requirement {path}: {exc}")


def _print_validation(exc: ValidationError) -> None:
    print("The requirement could not be accepted:", file=sys.stderr)
    for e in exc.errors:
        hint = f"  (did you mean: {', '.join(e.suggestions)}?)" if e.suggestions else ""
        print(f"  - {e.field}: {e.message}{hint}", file=sys.stderr)


def build_service(args) -> Service:
    cfg = load_config(args.config)
    ds = load_dataset(args.data)
    return Service(ds, cfg, audit_path=None if args.no_log else DEFAULT_LOG)


def cmd_match(svc: Service, args) -> int:
    raw = _requirement_arg(args)
    result = svc.match(raw, as_of=args.as_of, limit=args.limit)
    if args.json:
        print(json.dumps(result, indent=2, default=str))
        return 0
    if args.explain:
        text = llm_summary(result, AnthropicClient.from_env())
        print(text if text else "(plain-language summary unavailable; showing the standard report)\n")
    print(render_text(result, top=args.top))
    print(f"\nrequest {result['request_id']}  fingerprint {result['request_fingerprint'][:16]}")
    return 0


def cmd_ask(svc: Service, args) -> int:
    client = AnthropicClient.from_env()
    out = parse_free_text(args.text, client, svc.cfg)
    if not out.ok:
        print(out.message, file=sys.stderr)
        for e in out.errors:
            print(f"  - {e}", file=sys.stderr)
        return 2
    print("I understood this requirement:")
    for k, v in out.fields.items():
        print(f"  {k:<15} {v['value']}   [{v['source']}]")
    if out.requirement.inferred:
        print("  Fields marked [inferred] were not stated explicitly; please check them.")
    if not args.yes:
        if not sys.stdin.isatty():
            print("Not running automatically. Re-run with --yes to accept, or use `match` with a JSON file.")
            return 0
        if input("Run this match? [y/N] ").strip().lower() != "y":
            return 0
    result = svc.match(out.requirement, as_of=args.as_of, limit=args.limit)
    print()
    print(render_text(result, top=args.top))
    return 0


def cmd_why_not(svc: Service, args) -> int:
    out = svc.why_not(_requirement_arg(args), args.key, as_of=args.as_of)
    if args.json:
        print(json.dumps(out, indent=2))
        return 0
    if not out["found"]:
        print(out["message"] + (f". Did you mean: {', '.join(out['did_you_mean'])}?" if out["did_you_mean"] else ""))
        return 1
    for m in out["manufacturers"]:
        print(f"{m['key']}  {m['name']}: " + ("MATCHES" if m["matches"] else "excluded"))
        for c in m["criteria"]:
            print(f"   [{'ok' if c['passed'] else 'FAIL'}] {c['criterion']}: needs {c['requirement']} | has {c['actual']}")
    return 0


def cmd_quality(svc: Service, args) -> int:
    rep = svc.ds.quality_report()
    if args.json:
        print(json.dumps(rep, indent=2))
        return 0
    print(f"{rep['source']}\n  sha256 {rep['sha256'][:16]}...  rows read {rep['rows_read']}  "
          f"usable {rep['rows_usable']}  excluded {rep['rows_excluded']}\n")
    print(f"{'issue':<28}{'rows':>6}   action")
    for i in rep["issue_counts"]:
        print(f"{i['code']:<28}{i['count']:>6}   {i['action']}")
    print("\n" + rep["standing_note"])
    print("\nNot listed above because it depends on the date: expired certificates "
          f"(computed per request against as_of={svc.cfg.as_of}).")
    return 0


def cmd_demo(svc: Service, args) -> int:
    """The two-buyer scenario, end to end."""
    a, b = (json.loads((ROOT / "examples" / f"{n}.json").read_text()) for n in ("buyer_a", "buyer_b"))
    ra, rb = svc.submit("buyer-A", a), svc.submit("buyer-B", b)
    A, B = ra["result"], rb["result"]
    print(f"as of {A['as_of']}\nBuyer A (1,000 aluminium CNC, ISO 9001, 6 wks): {A['total_matches']} matches")
    print(f"Buyer B (1,500 stainless sheet metal, ISO 9001, 6 wks): {B['total_matches']} match(es)")
    overlap = sorted({m["key"] for m in svc.match(a, limit=1000)["matches"]} & {m["key"] for m in B["matches"]})
    print(f"Suppliers in both lists: {overlap or 'none'}")
    if not overlap:
        return 0
    key = overlap[0]
    print(f"\nBoth buyers want {key}. Buyer A commits first and places a hold:")
    print("  A:", svc.hold("buyer-A", key))
    print("Buyer B tries to commit to the same supplier:")
    try:
        svc.hold("buyer-B", key)
    except HoldRejected as exc:
        print("  B: rejected ->", exc)
    rb2 = svc.submit("buyer-B", b)
    print(f"Buyer B re-runs the search: {rb2['result']['total_matches']} matches")
    d = rb2["result"]["diagnosis"]
    if d:
        print("  B's funnel:", " -> ".join(f"{s['remaining']}" for s in rb2["result"]["funnel"]))
        for s in d["relaxations"][:2]:
            print(f"  suggestion: {s['suggestion']} -> {s['matches_if_relaxed']} match(es)")
    return 0


def cmd_serve(svc: Service, args) -> int:
    from .api import make_server
    server = make_server(svc, args.host, args.port)
    print(f"Listening on http://{args.host}:{args.port}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def build_parser() -> argparse.ArgumentParser:
    # Global options are accepted before OR after the subcommand (SUPPRESS keeps them from clobbering each other).
    base = argparse.ArgumentParser(add_help=False)
    base.add_argument("--data", default=argparse.SUPPRESS, help="manufacturer CSV")
    base.add_argument("--config", default=argparse.SUPPRESS, help="config.toml (default: repo config.toml)")
    base.add_argument("--no-log", action="store_true", default=argparse.SUPPRESS, help="do not write the audit log")
    p = argparse.ArgumentParser(prog="veridia", description="Veridia manufacturer matcher", parents=[base])
    p.set_defaults(data=str(DEFAULT_DATA), config=None, no_log=False)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, req=True):
        if req:
            g = sp.add_mutually_exclusive_group()
            g.add_argument("--example", help="name of a file in examples/ (e.g. buyer_de)")
            g.add_argument("--requirement", help="path to a requirement JSON file")
        sp.add_argument("--as-of", default=None, help="reference date YYYY-MM-DD (default: config.toml)")

    m = sub.add_parser("match", help="match a requirement", parents=[base]); common(m)
    m.add_argument("--limit", type=int, default=None); m.add_argument("--top", type=int, default=5)
    m.add_argument("--json", action="store_true"); m.add_argument("--explain", action="store_true",
                                                                  help="optional LLM-phrased summary")
    m.set_defaults(fn=cmd_match)

    a = sub.add_parser("ask", help="match a free-text requirement (needs ANTHROPIC_API_KEY)", parents=[base])
    a.add_argument("text"); common(a, req=False)
    a.add_argument("--limit", type=int, default=None); a.add_argument("--top", type=int, default=5)
    a.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    a.set_defaults(fn=cmd_ask)

    w = sub.add_parser("why-not", help="why was a manufacturer (not) matched?", parents=[base]); common(w)
    w.add_argument("key"); w.add_argument("--json", action="store_true"); w.set_defaults(fn=cmd_why_not)

    q = sub.add_parser("quality", help="data-quality report", parents=[base]); q.add_argument("--json", action="store_true")
    q.set_defaults(fn=cmd_quality)

    d = sub.add_parser("demo", help="run the two-buyer capacity scenario", parents=[base]); d.set_defaults(fn=cmd_demo)

    s = sub.add_parser("serve", help="start the HTTP API", parents=[base])
    s.add_argument("--host", default="127.0.0.1"); s.add_argument("--port", type=int, default=8000)
    s.set_defaults(fn=cmd_serve)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        svc = build_service(args)
        return args.fn(svc, args)
    except ValidationError as exc:
        _print_validation(exc)
        return 2
    except (ConfigError, DataError, NotFound) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def run() -> None:
    try:
        code = main()
        sys.stdout.flush()
    except BrokenPipeError:  # e.g. `veridia match ... | head`: not an error
        import os
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        code = 0
    sys.exit(code)
