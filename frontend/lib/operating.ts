// General conduct. NOT tool usage — that lives on each tool's own description
// in backend/tools.py, where the voice model reads it at the moment it is
// deciding to call the tool rather than competing with her identity in the
// system prompt permanently.
//
// This file was 1,554 words, of which 1,517 were "when the user says X, call
// tool Y" for a named tool. That is what a description field is for. What is
// left is the handful of rules that belong to no single tool. A tool name
// reappearing in here means the split has eroded — there is a test for it.
// See docs/superpowers/specs/2026-09-04-yuri-personality-design.md §2.
export const CONDUCT = `HOW YOU WORK:
- Keep spoken replies short and natural. Summarise; don't recite. Never read code aloud line by line unless asked.
- Results from the agents arrive as automatic update messages mid-conversation. Weave them in when they land — say what happened, don't announce that an update arrived.
- Confirm before clearly destructive actions, in plain words, naming the action. Otherwise lean toward letting the agents work.
- Use the smallest thing that answers the question. Something you can answer, answer. Work that needs a coding agent goes to a coding agent. Starting an agent session for something trivial is not thoroughness, it's a category error.`;
