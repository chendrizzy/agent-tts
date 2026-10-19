# Runner-allowlist announcement parser for pre-tool-use.sh. argv[1] = shell command.

def main():
    import re
    import sys

    cmd = sys.argv[1]

    # -----------------------------------------------------------------------
    # Step 1: Strip after first unquoted shell operator so patterns only see
    # the first command in a pipeline/chain. This prevents 'Running pytest on
    # 2>&1' / 'Running tests on null;' false announcements.
    # Split on unquoted: && || ; | > < 2>&1 2> &
    # Simple approach: split on the first occurrence of these operator tokens.
    _OP_RE = re.compile(r'\s*(?:&&|\|\||[|;&]|2>&1|2>|>|<)\s*')
    cmd = _OP_RE.split(cmd, maxsplit=1)[0].strip()

    # Strip leading shell preamble: env vars (KEY=val), sudo, exec, etc.
    cmd = re.sub(r'^(?:[A-Z_]+=\S+\s+)+', '', cmd).strip()
    cmd = re.sub(r'^(?:sudo|exec|time)\s+', '', cmd).strip()

    if not cmd:
        sys.exit(0)

    # -----------------------------------------------------------------------
    # Deny-list: shell operator tokens must not appear in extracted target.
    # If after stripping we still find operators (e.g., from quoted cmd), bail.
    SHELL_OPS = frozenset(['&&', '||', ';', '|', '>', '<', '2>&1', '2>', '&'])

    def is_operator(tok):
        return tok in SHELL_OPS or tok.startswith('>') or tok.startswith('<')

    def short_target(tok):
        '''Reduce a path or filename to a concise spoken form.'''
        if not tok or is_operator(tok):
            return ''
        # Strip leading ./
        tok = re.sub(r'^\./', '', tok)
        # If it is a path, take the basename
        if '/' in tok:
            tok = tok.rsplit('/', 1)[-1] or tok
        # Strip common test extensions
        tok = re.sub(r'\.(py|js|ts|rs|go|sh|md)$', '', tok)
        # Strip leading test_ prefix for readability
        tok = re.sub(r'^test_', '', tok)
        return tok or ''

    def safe_target_from_args(args_str):
        '''Extract first non-flag, non-operator token from an argument string.
        Returns empty string if none found or if token is a shell operator.
        '''
        for tok in args_str.split():
            if tok.startswith('-') or '=' in tok and '/' not in tok:
                continue
            if is_operator(tok):
                return ''
            return short_target(tok)
        return ''

    # -----------------------------------------------------------------------
    # ANNOUNCE-02: Only announce if an allowlisted runner is detected.
    # Order matters: most specific patterns first.

    # pytest
    m = re.search(r'\bpytest\b\s*(.*)', cmd)
    if m:
        args = m.group(1).strip()
        target = safe_target_from_args(args)
        if target:
            # If target looks like a directory (no extension), say 'in <dir>'
            if '.' not in target.split('/')[-1]:
                print(f'Running pytest in {target}.')
            else:
                print(f'Running pytest on {target}.')
        else:
            # No positional args — whole suite
            print('Running pytest.')
        sys.exit(0)

    # cargo test / cargo build / cargo run / cargo check / cargo clippy
    m = re.search(r'\bcargo\s+(test|build|run|check|clippy)\b(.*)', cmd)
    if m:
        sub = m.group(1)
        args = m.group(2).strip()
        rel = ' release' if '--release' in cmd else ''
        if sub == 'test':
            # cargo test <module::path>
            m2 = re.search(r'\bcargo\s+test\s+([\w:]+)', cmd)
            if m2:
                print(f'Running cargo test {m2.group(1)}.')
            else:
                print('Running cargo tests.')
        else:
            print(f'Cargo {sub}{rel}.')
        sys.exit(0)

    # npm test / npm run <script>
    m = re.search(r'\bnpm\s+test\b(.*)', cmd)
    if m:
        extra = m.group(1).strip()
        if '--watch' in extra or '-w' in extra:
            print('Running npm tests in watch mode.')
        else:
            print('Running npm tests.')
        sys.exit(0)
    m = re.search(r'\bnpm\s+run\s+(\S+)', cmd)
    if m:
        script = m.group(1)
        print(f'Running npm script {script}.')
        sys.exit(0)

    # jest / vitest / mocha  (standalone invocations)
    for runner in ('jest', 'vitest', 'mocha'):
        m = re.search(r'\b' + runner + r'\b\s*(.*)', cmd)
        if m:
            args = m.group(1).strip()
            target = safe_target_from_args(args)
            if target:
                print(f'Running {runner} on {target}.')
            else:
                print(f'Running {runner}.')
            sys.exit(0)

    # go test
    m = re.search(r'\bgo\s+test\b\s*(.*)', cmd)
    if m:
        args = m.group(1).strip()
        m2 = re.search(r'(\.\.\.|\S+)', args)
        if m2:
            print(f'Running go test {m2.group(1)}.')
        else:
            print('Running go tests.')
        sys.exit(0)

    # make <target>  — only if target looks test-like or is 'test'
    m = re.search(r'\bmake\s+([\w./-]+)', cmd)
    if m:
        target = m.group(1)
        # Only announce if target is 'test', 'tests', or contains 'test'
        if re.search(r'\btest', target, re.IGNORECASE):
            print(f'Running make {target}.')
            sys.exit(0)
        # Non-test make targets: announce only for well-known build targets
        if target in ('build', 'all', 'install', 'check', 'clean'):
            print(f'Running make {target}.')
            sys.exit(0)
        # Other make targets: silent
        sys.exit(0)

    # ANNOUNCE-02: No allowlist match — exit silently (no generic fallback).
    # Per ANNOUNCE-02: 'Running tests.' generic fallback is REMOVED.
    sys.exit(0)


if __name__ == "__main__":
    main()
