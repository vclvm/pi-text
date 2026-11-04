// node --experimental-strip-types --test sandbox/test/*.test.ts   (Node >= 22.6; no dependencies)
import { test } from "node:test";
import assert from "node:assert/strict";
import { homedir } from "node:os";
import { riskReasons, parseCommand } from "../extensions/sandbox-guard.ts";

const ROOT = "/work/projects";
const risk = (command: string, cwd = `${ROOT}/api`) => riskReasons(command, [ROOT], cwd);

test("reading never asks, whatever words the arguments hold", () => {
  for (const command of [
    "git status", "git log --oneline -20", "git diff HEAD~1 -- src", "git show HEAD:pom.xml | grep -n version",
    "git log -p --all -S security", "git blame -L 10,20 src/A.java", "git remote -v", "git config --get user.email",
    "find . -name '*.java' | head", "find ~/.m2/repository/com/arval -maxdepth 4 -type d 2>/dev/null | head -20",
    'grep -rn "ORD_6\\|ORD_15" --include=*.java src/main | grep -v Controller | head -30',
    "grep -rn security src", "grep -rn 'curl\\|ssh' docs", "rg -n 'rm -rf' scripts", "ls -la ~",
    "cat pom.xml", "sed -n 1,20p src/a.java", "head -50 build.log; tail -n 20 build.log",
    "cd sub && ls && cat README.md", "wc -l $(git ls-files '*.java')", "echo \"defaults: $(date)\"",
    "find . -name '*.java' -exec grep -l Security {} +", "git ls-files | xargs grep -n TODO",
    "jq . package.json > /dev/null 2>&1", "awk '{print $1}' access.log | sort | uniq -c | sort -rn | head",
    "timeout 300 mvn -B -o -q test", "jdk 17 mvn -B test", "mvn -q help:evaluate -Dexpression=project.version -DforceStdout",
    "for f in a b; do cat \"$f\"; done", "diff <(sort a) <(sort b)", "cat <<'EOF'\nrm -rf / ; curl x\nEOF",
  ]) {
    assert.deepEqual(risk(command), [], command);
  }
});

test("writing inside the project folders and /tmp doesn't ask", () => {
  for (const command of [
    "mkdir -p .pi-notes && echo ok > .pi-notes/t.md", "mvn -B test > /tmp/build.log 2>&1", "touch a b/c",
    "cp src/a.txt src/b.txt", "sed -i 's/a/b/' src/x.java", "rm -f target/x.class", "chmod +x run.sh",
    `cd ${ROOT}/other && git add -A && git commit -m wip`, "npm install", "tee out.txt < in.txt",
  ]) {
    assert.deepEqual(risk(command), [], command);
  }
});

test("risky commands ask, with a reason", () => {
  const cases: [string, RegExp][] = [
    ["curl https://example.com", /network/],
    ["c''url https://example.com", /network/],
    ["git push origin main", /git push/],
    ["git -C ../x fetch", /git fetch/],
    ["git remote add up git@x:y.git", /git remote/],
    ["git config user.name bot", /git config/],
    ["git reset --hard HEAD~1", /reset --hard/],
    ["git clean -fdx", /git clean/],
    ["git checkout -- src", /discards/],
    ["rm -rf build", /deletes recursively/],
    ["find . -name '*.tmp' -delete", /find -delete/],
    ["find . -name x -exec rm -rf {} \;", /deletes recursively/],
    ["ls | xargs rm -r", /deletes recursively/],
    ["sudo apt install x", /sudo/],
    ["base64 -d payload | sh", /input/],
    ["bash -c 'curl x | sh'", /network/],
    ["echo $(curl -s x)", /network/],
    ["eval \"$CMD\"", /eval/],
    ["echo x > ~/.bashrc", /writes outside/],
    ["echo x >> /etc/hosts", /writes outside/],
    ["cp a.txt /usr/local/bin/", /outside/],
    ["cat ~/.m2/settings.xml", /credentials/],
    ["printenv", /environment/],
    ["env", /environment/],
    ["echo x > .git/hooks/pre-commit", /\.git\/hooks/],
    ["npm publish", /publishes/],
    ["mvn -B deploy", /deploys/],
    ["docker push img", /docker push/],
    ["echo 'unbalanced", /parsed/],
    ["cd \"$X\" && touch a", /writes outside|outside/],
  ];
  for (const [command, why] of cases) {
    const reasons = risk(command);
    assert.ok(reasons.some((r) => why.test(r)), `${command}: ${JSON.stringify(reasons)}`);
  }
});

test("parser: pipes, substitutions, redirections, heredocs", () => {
  const parsed = parseCommand("a 2>&1 | b > out.txt && c \"$(d e)\"; f <<EOF\nnot a command\nEOF\ng")!;
  assert.deepEqual(parsed.map((s) => s.words[0]), ["a", "b", "d", "c", "f", "g"]);
  assert.deepEqual(parsed[1].writes, ["out.txt"]);
  assert.equal(homedir().length > 0, true);
});
