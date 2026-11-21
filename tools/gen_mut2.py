"""Single-token mutants in the style of the sample commits; also fixture and comment edits."""
import random, re, subprocess, os, sys
seed = int(sys.argv[2]); random.seed(seed); N = int(sys.argv[1])
repo = "/home/claude/br/sqlglot"; outdir = "/home/claude/br/mut2"; os.makedirs(outdir, exist_ok=True)
src_files = [l for l in subprocess.check_output(["git", "ls-files", "sqlglot"], cwd=repo, text=True).split() if l.endswith(".py")]
fix_files = [l for l in subprocess.check_output(["git", "ls-files", "tests/fixtures"], cwd=repo, text=True).split() if l.endswith(".sql") and "tpc" not in l]
W = [os.path.getsize(os.path.join(repo, f)) for f in src_files]
OPS = [
    (r" and ", lambda m: " or "), (r" or ", lambda m: " and "),
    (r" is not None", lambda m: " != None"), (r" is None", lambda m: " == None"),
    (r" is (?!not)", lambda m: " == "), (r"==", lambda m: "!="), (r"!=", lambda m: "=="),
    (r" < ", lambda m: " <= "), (r" > ", lambda m: " >= "), (r" <= ", lambda m: " < "), (r" >= ", lambda m: " > "),
    (r"\bTrue\b", lambda m: "False"), (r"\bFalse\b", lambda m: "True"),
    (r"\bnot ", lambda m: ""), (r"if (?!not)", lambda m: "if not "),
    (r"(?<![\w.])(\d+)(?![\w.])", lambda m: str(int(m.group(1)) + random.choice([1, -1]))),
    (r"seq_get\(args, (\d)\)", lambda m: f"seq_get(args, {int(m.group(1)) + 1})"),
]
made = 0
while made < N:
    kind = random.choices(["code", "fixture", "comment"], [6, 3, 1])[0]
    if kind == "fixture":
        f = random.choice(fix_files)
    else:
        f = random.choices(src_files, W)[0]
    p = os.path.join(repo, f); lines = open(p).read().split("\n")
    i = random.randrange(len(lines)); line = lines[i]; s = line.strip()
    if not s: continue
    if kind == "fixture":
        if s.startswith("#") or s.startswith("--"): continue
        toks = line.split(" ")
        j = random.randrange(len(toks))
        r = random.random()
        if r < 0.5: toks.insert(j, random.choice(["42", "0", "x", "NULL", "1"]))
        elif r < 0.8 and len(toks) > 1: toks[j] = toks[j][:-1] if len(toks[j]) > 1 else toks[j]
        else: toks[j] = toks[j] + random.choice([" ", "a", "1"])
        new = " ".join(toks)
    elif kind == "comment":
        ind = line[:len(line) - len(line.lstrip())]
        new = None
        lines.insert(i, ind + random.choice(["# Cheap check first", "# Kept for backwards compatibility", "# Keep in sync with the generator"]))
    else:
        if s.startswith(("#", "def ", "class ", "@", "import", "from", '"""')): continue
        op = random.choice(OPS)
        ms = list(re.finditer(op[0], line))
        if not ms: continue
        m = random.choice(ms)
        new = line[:m.start()] + op[1](m) + line[m.end():]
        if new == line: continue
    if new is not None:
        if new == line: continue
        lines[i] = new
    open(p, "w").write("\n".join(lines))
    try:
        ok = True
        if kind != "fixture":
            r = subprocess.run(["python", "-c", "import sqlglot, sqlglot.dialects, sqlglot.optimizer, sqlglot.executor"], cwd=repo, capture_output=True)
            ok = r.returncode == 0
        if ok:
            d = subprocess.check_output(["git", "diff"], cwd=repo, text=True)
            if d.strip():
                open(f"{outdir}/k{seed}_{made:03d}_{kind}.patch", "w").write(d); made += 1
    finally:
        subprocess.run(["git", "checkout", "-q", "--", f], cwd=repo)
print("made", made)
