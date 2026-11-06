---
name: ask-opus
description: Ask Opus for a decision, plan or diagnosis on a hard problem, paid from API credits instead of the subscription. Use when the user says "ask Opus", "get Opus's verdict", or runs /ask-opus.
---

Opus advises; this session does the work. Opus cannot read the repo or run anything, so its answer is only as good as the facts you send.

1. State one decision question. If the request holds several, ask them one at a time.
2. Gather the facts with the cheapest means you have (scouts or subagents, Grep, Read). Write them to a file in your scratchpad: the goal, constraints, what has been tried and how it failed, the options you see, and the code that matters as `file:line` plus the verbatim excerpt. Attach a whole file with `--attach` only when all of it matters. Never include secrets.
3. Run: `cpool advise ask --thread new --model claude-opus-5-5 --facts <file> [--attach <path> ...] --json "<question>"`
4. Exit 3 (`status: needs`): answer each item in `needs` the same cheap way, write the answers to a new facts file, then run `cpool advise ask --thread <thread> --facts <new file> --json "Answers to your NEEDS."`. Repeat until you get a verdict.
5. Exit 0: give the user the verdict in a few lines, then carry it out here as normal work. Exit 4: the monthly credit cap is reached; tell the user and stop asking. Exit 5: Opus refused; report it. Exit 2: report the error.

Never set or export ANTHROPIC_API_KEY. `cpool advise usage` shows this cycle's spend.
