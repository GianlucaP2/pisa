"""The eval with the real cheap model (PISA_EVAL_REAL=1, by hand: a paid call). The model gets Pisa's system
prompt, the language line and the case's turns before, no tools (pisa_tools/talk_eval.py's cases: on the
server the maintenance job runs them through Pisa's gateway). PISA_EVAL_MODEL names it ("openai:<model>"); the
provider's key comes from the environment, never from the repo."""

import os
from typing import Any

from pydantic_ai import Agent

from pisa.brain.conversation import asked_instruction, language_instruction, system_prompt
from pisa_tools.talk_eval import _history, latest_question


def ask_real(case: Any) -> str:
    model = os.environ["PISA_EVAL_MODEL"]
    agent = Agent(model, instructions=system_prompt(lang="it-IT"))
    result = agent.run_sync(
        case.words,
        message_history=_history(case),
        instructions=language_instruction(case.lang) + asked_instruction(latest_question(case)),
    )
    return str(result.output)
