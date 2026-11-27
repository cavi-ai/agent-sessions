"""Command-line interface for agent-sessions."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

from . import hooks, hosts, partners, paths, profiles, queue


def resource_parser(sub):
    resource = sub.add_parser(
        "resource", help="machine-consumed resource admission without peer messages"
    )
    resource.add_argument(
        "--session", help="logical owner retained by a supervised recovery receipt"
    )
    actions = resource.add_subparsers(dest="resource_action", required=True)
    actions.add_parser("protocol")
    request = actions.add_parser("request")
    request.add_argument("resource")
    request.add_argument("key")
    request.add_argument("--seconds", type=float, default=600)
    request.add_argument("--estimate-seconds", type=float)
    request.add_argument(
        "--resumable",
        action="store_true",
        help="retain the queued placeholder across process exit",
    )
    request.add_argument("--payload-sha256", help="bind the request key to exact job inputs")
    for action in ("status", "wait", "start", "finish", "cancel"):
        command = actions.add_parser(action)
        command.add_argument("ticket")
        if action == "wait":
            command.add_argument("--seconds", type=float, default=600)
            command.add_argument(
                "--keep",
                action="store_true",
                help="return a queued ticket when this wait window ends",
            )
        if action in ("start", "finish"):
            command.add_argument("--lease", required=True)
            command.add_argument("--fence", required=True, type=int)
        if action == "finish":
            command.add_argument(
                "--outcome", choices=("completed", "failed", "unresolved"), required=True
            )
            command.add_argument("--evidence")
    events = actions.add_parser("events")
    events.add_argument("--after", type=int, default=0)
    events.add_argument(
        "--lifecycle",
        action="store_true",
        help="include deduplicated launch-intent start signals",
    )


def _build_parser():
    parser = argparse.ArgumentParser(
        prog="agent-sessions",
        description=(
            "List active Codex, Claude Code, Antigravity, and OpenCode sessions, "
            "find sessions sharing a repository, and pass messages between them."
        ),
    )
    parser.add_argument(
        "--minutes",
        type=int,
        default=paths.ACTIVE_MINUTES,
        help="sessions updated within this many minutes count as active",
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--version", action="version", version="%(prog)s " + paths.VERSION)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("protocol", help="supported managed receipt contracts")
    listing = sub.add_parser("list", help="active sessions with cwd, worktree, branch and status")
    listing.add_argument("--reviews", action="store_true", help="include Codex approval reviewers")
    listing.add_argument(
        "--include-completed", action="store_true", help="include completed workers"
    )
    listing.add_argument("--host", choices=paths.HOST_CHOICES, help="show one native host")
    listing.add_argument("--match", help="match session and checkout details")
    sharing = sub.add_parser("who", help="active sessions in the same repository as PATH")
    sharing.add_argument("path", nargs="?", default=".")
    sharing.add_argument(
        "--include-completed", action="store_true", help="include completed workers"
    )
    sharing.add_argument("--host", choices=paths.HOST_CHOICES, help="show one native host")
    sharing.add_argument("--match", help="match session and checkout details")
    sending = sub.add_parser(
        "send", help="leave a message for a session id or every session in PATH"
    )
    sending.add_argument("to")
    sending.add_argument("text")
    sending.add_argument("--session", help="sender session id (default: this session)")
    reading = sub.add_parser("inbox", help="receive new messages for this session")
    reading.add_argument("--session", help="session id (default: this session)")
    reading.add_argument("--keep", action="store_true", help="preview without marking received")
    claiming = sub.add_parser("claim", help="take a message out of the queue and reply to its sender")
    claiming.add_argument("message_id")
    claiming.add_argument("text", nargs="?", default="ready", help="reply text (default: ready)")
    claiming.add_argument("--session", help="claiming session id (default: this session)")
    checking = sub.add_parser("status", help="whether a message is pending, received or claimed")
    checking.add_argument("message_id")
    hooking = sub.add_parser(
        "hook", help="read a Codex, Claude Code, Antigravity, or OpenCode hook payload"
    )
    hooking.add_argument("--host", choices=paths.HOST_CHOICES, help="receiving host")
    configuring = sub.add_parser("configure", help="install or remove hooks, preserving settings")
    configuring.add_argument("host", choices=(*paths.HOST_CHOICES, "all"))
    configuring.add_argument("--dry-run", action="store_true", help="print settings without writing")
    configuring.add_argument(
        "--remove", action="store_true", help="remove this installation's hooks"
    )
    configuring.add_argument(
        "--check", action="store_true", help="check whether hooks are registered without modifying"
    )
    profiling = sub.add_parser("profile", help="pin and launch native partner configuration")
    profile_actions = profiling.add_subparsers(dest="profile_action", required=True)
    creating_profile = profile_actions.add_parser("create")
    creating_profile.add_argument("host", choices=paths.HOST_CHOICES)
    creating_profile.add_argument("path")
    creating_profile.add_argument("name")
    creating_profile.add_argument(
        "--file", action="append", default=[], help="additional relative file to pin"
    )
    showing_profile = profile_actions.add_parser("show")
    showing_profile.add_argument("profile_id")
    launching_profile = profile_actions.add_parser("launch")
    launching_profile.add_argument("profile_id")
    launching_profile.add_argument("--resume", help="existing session bound to this profile")
    launching_profile.add_argument("--pair", help="pending profile partner id")
    launching_profile.add_argument("--dry-run", action="store_true")
    launching_profile.add_argument(
        "-p", "--print", dest="print_prompt", help="single prompt for headless execution (agy)"
    )
    launching_profile.add_argument(
        "--headless", action="store_true", help="run in non-interactive headless mode (agy)"
    )
    launching_profile.add_argument(
        "--output-format",
        choices=("text", "json", "stream-json"),
        help="output format for headless mode (agy)",
    )
    partnering = sub.add_parser("partner", help="invite or connect a partner session")
    actions = partnering.add_subparsers(dest="partner_action", required=True)
    for host in paths.HOSTS:
        aliases = ("antigravity",) if host == "agy" else ()
        creating = actions.add_parser(
            host, aliases=aliases, help=f"prepare a {host} partner in a directory"
        )
        creating.add_argument("path")
        creating.add_argument("brief")
        creating.add_argument("--session", help="caller session id (default: this session)")
        creating.add_argument("--profile", help="pinned native profile id")
        if host == "codex":
            creating.add_argument(
                "--project-id", help="saved local project id from codex_app list_projects"
            )
    connecting = actions.add_parser("connect", help="record the returned partner session id")
    connecting.add_argument("pair_id")
    connecting.add_argument("session_id", nargs="?", help="partner id (default: this session)")
    connecting.add_argument("--session", help="caller session id (default: this session)")
    connecting.add_argument("--host-id", help="local host id returned by Codex")
    showing = actions.add_parser("show", help="read a saved pair or pending creation request")
    showing.add_argument("pair_id")
    attaching = actions.add_parser(
        "attach", help="connect an existing worker without creating a session"
    )
    attaching.add_argument("session_id")
    attaching.add_argument("--session", help="caller session id (default: this session)")
    attaching.add_argument("--profile", help="require the existing worker profile binding")
    resuming = actions.add_parser("resume", help="resume the same partner with its pinned profile")
    resuming.add_argument("pair_id")
    resuming.add_argument("--session", help="caller session id (default: this session)")
    resuming.add_argument("--dry-run", action="store_true")
    resuming.add_argument(
        "-p", "--print", dest="print_prompt", help="single prompt for headless execution (agy)"
    )
    resuming.add_argument(
        "--headless", action="store_true", help="run in non-interactive headless mode (agy)"
    )
    resuming.add_argument(
        "--output-format",
        choices=("text", "json", "stream-json"),
        help="output format for headless mode (agy)",
    )
    completing = actions.add_parser("complete", help="complete a connected partner assignment")
    completing.add_argument("pair_id")
    completing.add_argument("--session", help="pair endpoint id (default: current session)")
    completing.add_argument(
        "--stop", action="store_true", help="stop a confirmed done Claude background job"
    )
    running = actions.add_parser("run", help="run one Codex partner turn without command hooks")
    running.add_argument("pair_id")
    running.add_argument("--timeout", type=float, default=60)
    running.add_argument("--prompt-file", help="canonical UTF-8 prompt file, at most 64 KiB")
    running.add_argument(
        "--local-provider", action="store_true", help="use the pinned loopback provider"
    )
    running.add_argument("--no-mcp", action="store_true", help="refuse any enabled MCP server")
    running.add_argument(
        "--complete", action="store_true", help="complete the pair after a successful turn"
    )
    running.add_argument(
        "--archive", action="store_true", help="archive the completed Codex session"
    )
    running.add_argument("--metrics", action="store_true", help="include native turn metrics")
    resource_parser(sub)
    return parser


def _handle_hook(args):
    env_sid = paths.environment_session_id()
    effective_host = hooks._detect_hook_host(
        {}, env_sid, profiles.normalize_host(args.host) if args.host else None
    )
    event = ""
    profiled = hooks.has_profile_context(env_sid)

    def _agy_block(msg):
        if event == "PreToolUse":
            print(json.dumps({"decision": "deny", "reason": msg}))
        else:
            print(
                json.dumps(
                    {
                        "injectSteps": [{"errorMessage": {"userMessage": msg}}],
                        "terminationBehavior": "terminate",
                    }
                )
            )

    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            raise TypeError("hook payload must be a JSON object")
        sid = (
            payload.get("session_id")
            or payload.get("thread_id")
            or payload.get("conversationId")
        )
        event = payload.get("hook_event_name") or payload.get("hookEventName") or ""
        effective_host = hooks._detect_hook_host(payload, sid, effective_host)
        profiled = profiled or hooks.has_profile_context(sid)
        if profiled and not sid:
            raise profiles.ProfileError("profiled hook requires a native session id")
        result = hooks.hook(payload, args.minutes, effective_host)
    except profiles.ProfileError as exc:
        msg = f"agent-sessions profile: {exc}"
        print(msg, file=sys.stderr)
        if effective_host == "agy":
            _agy_block(msg)
            return 0
        return 2
    except Exception as exc:  # ordinary message hooks remain non-blocking
        msg = f"agent-sessions hook: {exc}"
        print(msg, file=sys.stderr)
        if profiled:
            if effective_host == "agy":
                _agy_block(msg)
                return 0
            return 2
        if effective_host == "agy":
            print(json.dumps({"decision": "allow"} if event == "PreToolUse" else {}))
        return 0
    if result is not None:
        print(json.dumps(result))
    return 0


def _main(argv=None):
    args = _build_parser().parse_args(argv)

    if args.command == "resource":
        from . import resource_broker

        result = resource_broker.dispatch(args, paths.data_dir(), paths.environment_session_id())
        print(json.dumps(result))
        return 0

    if args.command == "protocol":
        print(
            json.dumps(
                {
                    "version": 1,
                    "managed_run": {
                        "mcp_call_receipts": "canonical-json-sha256",
                        "maximum_mcp_calls": 64,
                        "memory_context": "disabled-before-first-turn",
                    },
                }
            )
        )
        return 0

    if args.command == "partner" and args.partner_action == "run":
        from .managed_runs import run

        if args.archive and not args.complete:
            raise ValueError("--archive requires --complete")
        result = run(
            args.pair_id,
            args.timeout,
            args.prompt_file,
            args.no_mcp,
            args.local_provider,
            complete=args.complete,
            archive=args.archive,
            metrics=args.metrics,
        )
        print(json.dumps(result, indent=2))
        return (
            0
            if result["status"] == "completed"
            and (
                not result.get("completion")
                or result["completion"].get("status") == "completed"
            )
            else 1
        )

    if args.command == "profile":
        if args.profile_action == "create":
            result = profiles.capture(args.host, args.path, args.name, args.file)
            paths.write_json(paths.data_dir() / "profiles" / (result["id"] + ".json"), result)
        elif args.profile_action == "show":
            result = profiles.load(paths.data_dir(), args.profile_id)
        else:
            headless = args.headless or args.print_prompt is not None
            result = partners.launch_profile(
                args.profile_id,
                args.resume,
                args.pair,
                args.dry_run,
                prompt=args.print_prompt,
                headless=headless,
                output_format=args.output_format,
            )
        print(json.dumps(result, indent=2))
        return 0

    if args.command == "partner" and args.partner_action == "complete":
        result = partners.complete_partner(
            args.pair_id, args.session or paths.environment_session_id(), args.stop
        )
        print(json.dumps(result, indent=2))
        return 0

    if args.command == "partner" and args.partner_action == "show":
        print(json.dumps(partners.read_pair(args.pair_id), indent=2))
        return 0

    if args.command == "status":
        result = queue.status(args.message_id)
        print(
            json.dumps(result, indent=2)
            if args.json
            else f"{result['id']} {result['state']}: "
            + ", ".join(result.get("received_by") or [result.get("claimed_by") or "nobody yet"])
        )
        return 0

    if args.command == "claim":
        session_id = args.session or paths.environment_session_id()
        if not session_id:
            session_id = hosts.self_id(os.getcwd(), hosts.sessions(args.minutes))
        paths.validate_session_id(session_id)
        path = queue.find_message(args.message_id, session_id, os.getcwd())
        if path is None:
            cwd = next(
                (s["cwd"] for s in hosts.sessions(args.minutes) if s["id"] == session_id), None
            )
            path = queue.find_message(args.message_id, session_id, cwd) if cwd else None
        result = queue.claim(
            args.message_id,
            path,
            session_id,
            args.text,
            f"session {session_id[:8]} in {paths.home(os.getcwd())}",
        )
        reply = result["reply"]
        print(
            json.dumps(result, indent=2)
            if args.json
            else f"Claimed {args.message_id}" + (f"; replied to {reply['to']}" if reply else "")
        )
        return 0

    if args.command == "configure":
        targets = list(paths.HOSTS) if args.host == "all" else [args.host]
        if args.check:
            if args.remove or args.dry_run:
                raise ValueError("--check cannot be combined with --remove or --dry-run")
            statuses = [hooks.hook_status(target) for target in targets]
            if args.json:
                print(json.dumps(statuses if args.host == "all" else statuses[0], indent=2))
            else:
                for item in statuses:
                    if item["installed"]:
                        print(f"{item['host']}: ok ({item['path']})")
                    else:
                        detail = item["error"] or (
                            "missing " + ", ".join(item["missing"])
                        )
                        print(f"{item['host']}: not configured ({item['path']}): {detail}")
            return 0 if all(item["installed"] for item in statuses) else 1
        if args.host == "all":
            results = {}
            for target in targets:
                path, config = hooks.configure_hooks(target, args.dry_run, args.remove)
                results[target] = config
                if not args.dry_run and not args.json:
                    print(
                        f"{'Removed' if args.remove else 'Installed'} "
                        f"agent-sessions hooks in {path}"
                    )
                    if target == "codex" and not args.remove:
                        print("Review and trust the hooks in Codex Settings > Hooks or CLI /hooks.")
                    if target == "opencode":
                        print(
                            "Run `opencode reload` to apply the plugin change to the running service."
                        )
            if args.dry_run or args.json:
                print(json.dumps(results, indent=2))
            return 0
        path, config = hooks.configure_hooks(args.host, args.dry_run, args.remove)
        if args.dry_run or args.json:
            print(json.dumps(config, indent=2))
        else:
            print(f"{'Removed' if args.remove else 'Installed'} agent-sessions hooks in {path}")
            if args.host == "codex" and not args.remove:
                print("Review and trust the hooks in Codex Settings > Hooks or CLI /hooks.")
            if args.host == "opencode":
                print("Run `opencode reload` to apply the plugin change to the running service.")
        return 0

    if args.command == "hook":
        return _handle_hook(args)

    selected_host = (
        profiles.normalize_host(args.host)
        if args.command in ("list", "who") and args.host
        else None
    )
    found = hosts.sessions(args.minutes, getattr(args, "reviews", False), host=selected_host)
    if args.command == "partner":
        if args.partner_action == "connect":
            caller_host, env_id = paths.environment_session()
            caller = args.session or env_id
            if caller is None:
                current = hosts.session_in_directory(os.getcwd(), found)
                caller, caller_host = current["id"], current["host"]
            elif caller != env_id:
                caller_host = None
            elif caller_host is None:
                raise ValueError("caller session is ambiguous across host environment variables")
        else:
            caller = args.session or hosts.self_id(os.getcwd(), found)
        paths.validate_session_id(caller)
        if args.partner_action == "attach":
            result = partners.attach_partner(args.session_id, caller, found, args.profile)
        elif args.partner_action == "connect":
            result = partners.connect_partner(
                args.pair_id, args.session_id or caller, caller, found, args.host_id,
                caller_host=caller_host,
            )
        elif args.partner_action == "resume":
            pair = partners.read_pair(args.pair_id)
            if caller not in (pair["caller"]["id"], pair["partner"]["id"]):
                raise ValueError("only a pair endpoint can resume the partner")
            if not pair.get("profile") or pair["status"] != "connected":
                raise ValueError("resume requires a connected profiled partner")
            headless = args.headless or args.print_prompt is not None
            result = partners.launch_profile(
                pair["profile"],
                pair["partner"]["id"],
                pair["id"],
                args.dry_run,
                prompt=args.print_prompt,
                headless=headless,
                output_format=args.output_format,
            )
        else:
            result = partners.create_partner(
                args.partner_action,
                args.path,
                args.brief,
                caller,
                found,
                getattr(args, "project_id", None),
                args.profile,
            )
        print(json.dumps(result, indent=2))
        return 0

    if args.command in ("list", "who") and not args.include_completed:
        hidden = partners.completed_workers(partners.all_pairs())
        found = [row for row in found if (row["host"], row["id"]) not in hidden]
    if args.command in ("list", "who"):
        found = [row for row in found if hosts.matches_session(row, args.match)]
    current_id = (
        paths.environment_session_id() if args.command in ("list", "who", "send") else None
    )
    if args.command == "list":
        result = found
        empty = (
            "No matching recent sessions."
            if args.host or args.match is not None
            else f"No sessions active in the last {args.minutes} minutes."
        )
        text = [hosts.describe(s, full_id=True, current_id=current_id) for s in found] or [empty]
    elif args.command == "who":
        result = hosts.who(args.path, found)
        kind = "worktree" if result["linked"] else "main checkout"
        text = [
            f"{paths.home(result['root'])} ({kind}, {result['branch']}, {result['dirty']} uncommitted)"
        ]
        for peer in result["sessions"]:
            mark = "SAME WORKTREE " if peer["same_worktree"] else ""
            text.append(f"- {mark}{hosts.describe(peer, full_id=True, current_id=current_id)}")
            if peer["overlap"]:
                text.append(f"  also changing: {', '.join(peer['overlap'][:10])}")
        if not result["sessions"]:
            text.append(
                "No matching recent sessions."
                if args.host or args.match is not None
                else "No other active session works in this repository."
            )
    elif args.command == "send":
        sender = args.session or current_id
        if sender:
            paths.validate_session_id(sender)
        name = f"{f'session {sender[:8]}' if sender else 'an agent'} in {paths.home(os.getcwd())}"
        message, targets = queue.send(args.to, args.text, found, sender, name)
        result = {"message": message, "to": [t["id"] for t in targets]}
        waiting = (
            f" and any session that starts under {paths.home(message['path'])}"
            if "path" in message
            else ""
        )
        text = [f"Left message {message['id']} for {len(targets)} active session(s){waiting}:"]
        text += [f"- {hosts.describe(t, current_id=current_id)}" for t in targets]
    else:
        env_host, env_id = paths.environment_session()
        session_id = args.session or hosts.self_id(os.getcwd(), found)
        cwd = next((s["cwd"] for s in found if s["id"] == session_id), os.getcwd())
        host = env_host if session_id == env_id and env_host else None
        if host is None:
            host = next((s["host"] for s in found if s["id"] == session_id), None)
        result = queue.receive(session_id, cwd, mark=not args.keep, host=host)
        text = [f"{m['id']} {m['at']} {m['from']}: {m['text']}" for m in result] or [
            "No new messages."
        ]
    print(json.dumps(result, indent=2) if args.json else "\n".join(text))
    return 0


def main(argv=None):
    try:
        return _main(argv)
    except (OSError, ValueError, TypeError, sqlite3.Error) as exc:
        print(f"agent-sessions: {exc}", file=sys.stderr)
        return 1
