# Шаблон запроса для ревью

Запрос пишется по-английски, отчёт запрашивается по-русски. Замени всё в угловых скобках. Блоки проверок бери по таблице из `SKILL.md`; лишние удали.

Оглавление: постоянная часть · блоки проверок · формат отчёта.

## Постоянная часть

```
You are an independent reviewer with a clean context. Review <what: a pull request / N pull requests / a set of documents> in a repository for a take-home test assignment. You did not write it; be skeptical and concrete.

This is a READ-ONLY review. Do NOT edit, create or delete files. Do NOT run git or gh commands that change state (no commit, push, merge, comment, approve). Do NOT run terraform, docker, ssh, or anything that contacts a cloud provider, a model API, or creates resources. Do NOT read or print any .env file: it contains secrets.

## Where things are

Main checkout (branch main): <absolute path>
<for each change under review:>
- <PR number and title> — worktree: <absolute path>; see the change with: git -C <worktree> diff origin/main
<or, for whole documents: the list of files>

All documents are in Russian. Background, read as needed:
- docs/business/TASK.md — the employer's assignment. Source of truth: everything else must satisfy it.
- docs/architecture/: ARCHITECTURE.md (deterministic part: storage, access control, write path, turn budget, API), AGENT.md (what the model sees), RETRIEVAL.md, DECISIONS.md (decision log), INFRASTRUCTURE.md.
- docs/development/: TESTING.md, AI_TESTING.md, STAND.md, JOURNAL.md.
- CLAUDE.md — rules for the coding agent, including limits and the git policy.

## Context

<state of the project: what exists, what does not>
The employer will run the service with `make run` / `docker compose up` on THEIR OWN dataset of the same format, with their own hidden questions and their own model (any OpenAI-compatible open model with tool calling), run `make test` and `make eval`, and then discuss and modify the solution live in an interview for a Tech Lead position. The repository is PUBLIC on GitHub.

<decisions the author made deliberately and does not want re-litigated, e.g. scope and time are not a concern>

<what the author says was verified and how — as a claim to check against the code, not as a fact>

<for a repeat review: what the author says was fixed since the previous review; verify each fix is present, coherent, and did not introduce new problems>
```

## Блоки проверок

### Соответствие заданию

```
**Compliance with TASK.md**, requirement by requirement: the four rules (access control in code before data reaches the model; budget enforced when assembling input; write only after confirmation and exactly once; measurability), the data contract, the POST /turn response contract and the confirm/reject table, budget limits, the seven mandatory tests, the `make data` / `make run` / `make test` / `make eval` contracts, the deliverables, "code must not depend on dataset contents", "tests without network and without model". Flag anything that would fail on the employer's dataset, hidden questions or model. Walk through each question type in the assignment's question table and say what outcome the described rules produce when the model behaves reasonably and when it behaves badly.
```

### Противоречия

```
**Contradictions**: places where one document or file says X and another says not-X; stale statements left from an earlier plan; a mechanism described in one place and silently assumed differently in another; mismatched names (tools, statuses, env vars, tables, endpoints, states), counts, limits and prices; anything CLAUDE.md tells the agent that conflicts with the architecture or the decision log. Grep for leftovers of superseded decisions and quote each with file and line. Check decision-log conventions: old decisions are not rewritten, new ones are appended, the index reflects the current status.
```

### Механизмы

```
**Correctness of the described mechanisms**: exactly-once write (state machine, crash and concurrency cases, external system errors); access control (both barriers, what can leak into model input through history, aggregates, error messages, truncation references, citations, diagnostic endpoints); outcome rules; budget checks and token estimation (can a call still exceed the limits); session capture and atomic turn; dataset loading and reload. For each mechanism look for a concrete sequence of events that produces a wrong result.
```

### Реализуемость

```
**Whether an implementer could build from this**: where two reasonable engineers reading the documents would build different things because something essential is undefined, name it. Skip what is legitimately left to code.
```

### Гарантии в коде

```
**Guarantees in code**: is access filtering applied in code before data becomes a tool result, on every path including direct id and explicit requests; is the budget checked before a model call is sent; is the external write reachable only from the confirmed transition, with the stored idempotency key; can any secret reach model input, API responses or logs; does anything in the service code contain dataset contents (ids, numbers, names); does behaviour match the tables in ARCHITECTURE.md row by row — name each row that the code contradicts.
```

### Тесты

```
**Tests**: do they assert on the model input recorded by the stub rather than on answer text; does every row of the guarantee tables in ARCHITECTURE.md have a test; do tests depend on the contents of data/; are there sleeps or wall-clock dependencies; would each test fail if the guarantee it names were removed — point out tests that would stay green.
```

### Инфраструктура и скрипты

```
**Infrastructure and scripts**: resource dependencies and ordering, including destroy; lookups that fail obscurely on zero or several matches; unexpected destructive replacement when a variable changes; what is exposed to the internet and to whom, given the service has no authentication; how secrets are passed (arguments visible in process listings, files executed as code, values in state or plan output, container environment); .gitignore coverage; pinned versions and where the provider is installed from; shell portability (the bash 3.2 that ships with macOS, no terminal, Linux CI), quoting, behaviour when a file is missing or values contain spaces, quotes or tildes. Check every statement in the runbook against the code.
```

### Прогон и метрики

```
**Eval harness and metrics**: do the checks implement the assignment's comparison rules exactly; is a leak counted in pending_action as well as in the answer; are sessions isolated between attempts and runs; can the harness obtain every number it reports while treating the service as a black box; are conclusions stated more strongly than the sample supports; is anything from the dataset hardcoded into prompts or code to make a question pass.
```

### Несколько изменений сразу

```
**Merge order and conflicts**: which files do the changes share; do they merge cleanly in either order; does one reference something that exists only after the other.
```

## Формат отчёта

```
## Technical claims

Where the change states facts about PostgreSQL, Docker, Terraform, a cloud provider, FastAPI, model servers or shells, check them against what you know. Say which are wrong, which are right, and which you could not verify. Do not present a guess as verified.

## How to report

Reply in Russian. One ranked list, most severe first. For each finding:
- severity: critical (would fail the employer's check, break a guarantee, or expose a secret) / major (real risk, or a contradiction that will mislead) / minor;
- where: file and line or section, quoting the exact line(s); for contradictions quote both sides;
- what is wrong, with a concrete scenario (input or sequence of events → wrong result);
- a suggested fix in one or two sentences; for code, name the exact change.

Then two short sections: (a) what you checked and found sound; (b) what you could not verify without running things, and which facts you are stating from memory rather than from the files.

Do not pad: skip wording, formatting and style remarks unless they would mislead an implementer or operator. If there are few real problems, say so rather than inventing more.
```
