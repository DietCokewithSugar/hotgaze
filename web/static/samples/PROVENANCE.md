# Sample focus group sessions

The AI focus group page (`/focus-group`) and the home page's excerpt show one
sample session, in the language the page is set to:

| File | What it is |
|---|---|
| `focus-group-wealth.zh.json` | 投资理财用户的痛点与需求: five participants, four questions, 37 turns, structured report. |
| `focus-group-wealth.en.json` | The same session translated into English. |

**Who wrote them.** Both were written by Claude (an AI assistant made by
Anthropic) while the tool was being built, before a DeepSeek key was available.
They are not DeepSeek output. Claude followed the tool's own pipeline: the plan
has the shape `/api/focus-group/plan` returns, the turns follow the discussion
schedule in `web/focus_group.py` (rotating speaking order, one probe per
question at most, a closing round), and every quote in the report is the
participant's exact words. `tests/test_focus_group.py` replays both files
through that pipeline and fails if any of this stops being true.

Each file says so in its `generator` field (`claude-simulated`), and the page
reads that field to label the sample accordingly. No real people, interviews or
survey data are behind them.

**Replacing them with a real run.** Run a session on the deployed site, choose
**下载数据 / Download data** once the report is ready, and save the file here as
`focus-group-wealth.zh.json` (or `.en.json` for an English session). The export
records `"generator": "deepseek:<model>"`, so the page then labels the sample as
a real run of the tool. Keep the `transcript` turn order as exported.
