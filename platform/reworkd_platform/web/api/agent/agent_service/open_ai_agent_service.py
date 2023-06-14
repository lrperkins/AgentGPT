from typing import List, Optional

from lanarky.responses import StreamingResponse
from langchain.output_parsers import PydanticOutputParser
from langchain.prompts import ChatPromptTemplate, SystemMessagePromptTemplate
from loguru import logger

from reworkd_platform.schemas import ModelSettings
from reworkd_platform.web.api.agent.agent_service.agent_service import AgentService
from reworkd_platform.web.api.agent.analysis import Analysis
from reworkd_platform.web.api.agent.helpers import (
    call_model_with_handling,
    openai_error_handler,
    parse_with_handling,
)
from reworkd_platform.web.api.agent.model_settings import create_model
from reworkd_platform.web.api.agent.prompts import (
    analyze_task_prompt,
    create_tasks_prompt,
    start_goal_prompt,
)
from reworkd_platform.web.api.agent.task_output_parser import TaskOutputParser
from reworkd_platform.web.api.agent.tools.open_ai_function import analysis_function
from reworkd_platform.web.api.agent.tools.tools import (
    get_tool_from_name,
    get_user_tools,
)
from reworkd_platform.web.api.memory.memory import AgentMemory


class OpenAIAgentService(AgentService):
    def __init__(self, model_settings: ModelSettings, agent_memory: AgentMemory):
        self.model_settings = model_settings
        self.agent_memory = agent_memory
        self._language = model_settings.language or "English"

    async def start_goal_agent(self, *, goal: str) -> List[str]:
        completion = await call_model_with_handling(
            self.model_settings,
            ChatPromptTemplate.from_messages(
                [SystemMessagePromptTemplate(prompt=start_goal_prompt)]
            ),
            {"goal": goal, "language": self._language},
        )

        task_output_parser = TaskOutputParser(completed_tasks=[])
        tasks = parse_with_handling(task_output_parser, completion)

        with self.agent_memory as memory:
            memory.reset_class()
            memory.add_tasks(tasks)

        return tasks

    async def analyze_task_agent(
        self, *, goal: str, task: str, tool_names: List[str]
    ) -> Analysis:
        model = create_model(self.model_settings)
        message = await openai_error_handler(
            model_settings=self.model_settings,
            func=model.apredict_messages,
            messages=analyze_task_prompt.format_prompt(
                goal=goal,
                task=task,
                language=self._language,
            ).to_messages(),
            functions=[analysis_function(get_user_tools(tool_names))],
        )

        function_call = message.additional_kwargs["function_call"]
        completion = function_call["arguments"]

        pydantic_parser = PydanticOutputParser(pydantic_object=Analysis)
        return parse_with_handling(pydantic_parser, completion)

    async def execute_task_agent(
        self,
        *,
        goal: str,
        task: str,
        analysis: Analysis,
    ) -> StreamingResponse:
        print("Execution analysis:", analysis)

        tool_class = get_tool_from_name(analysis.action)
        return await tool_class(self.model_settings).call(goal, task, analysis.arg)

    async def create_tasks_agent(
        self,
        *,
        goal: str,
        tasks: List[str],
        last_task: str,
        result: str,
        completed_tasks: Optional[List[str]] = None,
    ) -> List[str]:
        completion = await call_model_with_handling(
            self.model_settings,
            ChatPromptTemplate.from_messages(
                [SystemMessagePromptTemplate(prompt=create_tasks_prompt)]
            ),
            {
                "goal": goal,
                "language": self._language,
                "tasks": "\n".join(tasks),
                "lastTask": last_task,
                "result": result,
            },
        )

        previous_tasks = (completed_tasks or []) + tasks
        tasks = [completion] if completion not in previous_tasks else []

        unique_tasks = []
        with self.agent_memory as memory:
            for task in tasks:
                similar_tasks = memory.get_similar_tasks(task)

                # Check if similar tasks are found
                if not similar_tasks:
                    unique_tasks.append(task)
                else:
                    logger.info(f"Similar tasks to '{task}' found: {similar_tasks}")

            if unique_tasks:
                memory.add_tasks(unique_tasks)

        return unique_tasks
