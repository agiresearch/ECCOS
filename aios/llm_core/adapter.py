from aios.context.simple_context import SimpleContextManager
from aios.llm_core.strategy import RouterStrategy, SimpleStrategy
from aios.llm_core.local import HfLocalBackend, VLLMLocalBackend, OllamaBackend
from aios.utils.id_generator import generator_tool_call_id
from cerebrum.llm.apis import LLMQuery, LLMResponse
from litellm import completion
import json

from typing import Dict, Optional, Any, List
import time
import re
import os
from aios.config.config_manager import config

class LLMAdapter:
    """
    The LLMAdapter class is an abstraction layer that represents the LLM
    router. This router allows load-balancing of multiple varying endpoints so
    that multiple requests can be handled at once.

    Args:
        llm_name (str or List[str])     : Name of the LLMs. If a string is
                                          provided, then only one LLM will be
                                          activated.
        max_gpu_memory (dict, optional) : Maximum GPU resources that can be 
                                          allocated to the LLMs.
        eval_device (str, optional)     : Evaluation device of binding LLM to
                                          designated devices for inference.
        max_new_tokens (int, optional)  : Maximum token length generated by the
                                          LLM. Defaults to 256.
        log_mode (str, optional)        : Mode of logging the LLM processing
                                          status. Defaults to "console".
        llm_backend (str, optional)     : Backend to use for speeding up 
                                          open-source LLMs. Defaults to None.
                                          Choices are ["vllm", "ollama"]
    """

    def __init__(
        self,
        llm_configs: List[Dict[str, Any]],
        api_key: str | List[str] | None = None,
        log_mode: str = "console",
        use_context_manager: bool = False,
        strategy: Optional[RouterStrategy] = RouterStrategy.SIMPLE,
    ):
        """Initialize the LLM with the specified configuration.
        Args:
            llm_configs (List[Dict[str, Any]]): List of LLM configurations containing model details
            api_key (str | List[str] | None): DEPRECATED. Originally for API keys, now handled by environment variables
            log_mode (str): Logging mode, defaults to "console" 
            use_context_manager (bool): Whether to use context manager for handling LLM context
            strategy (Optional[RouterStrategy]): Strategy for routing requests between LLMs, defaults to SIMPLE
        """
        # if isinstance(llm_name, list) != isinstance(llm_backend, list):
        #     raise ValueError("llm_name and llm_backend do not be the same type")
        # elif isinstance(llm_backend, list) and len(llm_name) == len(llm_backend):
        #     raise ValueError("llm_name and llm_backend do not have the same length")

        self.log_mode = log_mode
        self.context_manager = SimpleContextManager() if use_context_manager else None
        self.llm_configs = llm_configs

        # Set all supported API keys
        api_providers = {
            "openai": "OPENAI_API_KEY",
            "gemini": "GEMINI_API_KEY",
            "groq": "GROQ_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
            "huggingface": "HF_AUTH_TOKEN"
        }
        
        print("\n=== LLMAdapter Initialization ===")
        # print(f"Initializing LLM with name: {llm_name}")
        # print(f"Backend: {llm_backend}")
        
        # Prioritize getting API keys from config and set them to environment variables
        for provider, env_var in api_providers.items():
            print(f"\nChecking {provider} API key:")
            # First check the configuration file
            api_key = config.get_api_key(provider)
            if api_key:
                print(f"- Found in config.yaml, setting to environment")
                os.environ[env_var] = api_key
                if provider == "huggingface":
                    os.environ["HUGGING_FACE_API_KEY"] = api_key
                    print("- Also set HUGGING_FACE_API_KEY")
            # else:
            #     # If not found in the configuration file, check the environment variables
            #     if env_var in os.environ:
            #         print(f"- Not found in config.yaml, using environment variable: {env_var}=****")
            #     else:
            #         print(f"- Not found in config.yaml or environment variables")

        # breakpoint()
        
        self.llms = []
        
        # breakpoint()
        
        # Format model names to match backend or instantiate local backends
        for idx in range(len(self.llm_configs)):
            # if self.llm_backend[idx] is None:
            #     continue
            llm_name = self.llm_configs[idx].get("name", None)
            llm_backend = self.llm_configs[idx].get("backend", None)
            max_gpu_memory = self.llm_configs[idx].get("max_gpu_memory", None)
            eval_device = self.llm_configs[idx].get("eval_device", None)
            hostname = self.llm_configs[idx].get("hostname", None)

            match llm_backend:
                case "hflocal":
                    if "HUGGING_FACE_API_KEY" not in os.environ:
                        raise ValueError("HUGGING_FACE_API_KEY not found in config or environment variables")
                    
                    # self.llm_name[idx] = HfLocalBackend(
                    #     self.llm_name[idx],
                    #     max_gpu_memory=max_gpu_memory,
                    #     hostname=hostname
                    # )
                    self.llms.append(HfLocalBackend(
                        llm_name,
                        max_gpu_memory=max_gpu_memory,
                        hostname=hostname
                    ))
                    
                case "vllm":
                    self.llms.append(VLLMLocalBackend(
                        llm_name,
                        max_gpu_memory=max_gpu_memory,
                        hostname=hostname
                    ))
                    
                case "ollama":
                    self.llms.append(OllamaBackend(
                        llm_name,
                        hostname=hostname
                    ))
                case None:
                    continue
                case _:
                    if self.llm_configs[idx]["backend"] == "google":
                        self.llm_configs[idx]["backend"] = "gemini"
                        
                    # Google backwards compatibility fix
                    
                    prefix = self.llm_configs[idx]["backend"] + "/"
                    is_formatted = llm_name.startswith(prefix)
                    
                    # if not is_formatted:
                    # self.llm_name[idx] = "gemini/" + self.llm_name[idx].split("/")[1]
                    # continue
                    
                    if not is_formatted:
                        self.llms.append(prefix + llm_name)
        
        for llm_config in self.llm_configs:
            print("Initialized LLM:", llm_config)
        
        if strategy == RouterStrategy.SIMPLE:
            self.strategy = SimpleStrategy(self.llm_configs)

    def tool_calling_input_format(self, messages: list, tools: list) -> list:
        """Integrate tool information into the messages for open-sourced LLMs

        Args:
            messages (list): messages with different roles
            tools (list): tool information
        """
        prefix_prompt = (
            "In and only in current step, you need to call tools. Available tools are: "
        )
        tool_prompt = json.dumps(tools)
        suffix_prompt = "".join(
            [
                "Must call functions that are available. To call a function, respond "
                "immediately and only with a list of JSON object of the following format:"
                '[{"name":"function_name_value","parameters":{"parameter_name1":"parameter_value1",'
                '"parameter_name2":"parameter_value2"}}]'
            ]
        )

        # translate tool call message for models don't support tool call
        for message in messages:
            if "tool_calls" in message:
                message["content"] = json.dumps(message.pop("tool_calls"))
                
            elif message["role"] == "tool":
                message["role"] = "user"
                tool_call_id = message.pop("tool_call_id")
                content = message.pop("content")
                message["content"] = (
                    f"The result of the execution of function(id :{tool_call_id}) is: {content}. "
                )

        messages[-1]["content"] += prefix_prompt + tool_prompt + suffix_prompt
        return messages

    def parse_json_format(self, message: str) -> str:
        json_array_pattern = r"\[\s*\{.*?\}\s*\]"
        json_object_pattern = r"\{\s*.*?\s*\}"

        match_array = re.search(json_array_pattern, message)

        if match_array:
            json_array_substring = match_array.group(0)

            try:
                json_array_data = json.loads(json_array_substring)
                return json.dumps(json_array_data)
            except json.JSONDecodeError:
                pass

        match_object = re.search(json_object_pattern, message)

        if match_object:
            json_object_substring = match_object.group(0)

            try:
                json_object_data = json.loads(json_object_substring)
                return json.dumps(json_object_data)
            except json.JSONDecodeError:
                pass
        return "[]"

    def parse_tool_calls(self, message):
        # add tool call id and type for models don't support tool call
        # if isinstance(message, dict):
        #     message = [message]
        tool_calls = json.loads(self.parse_json_format(message))
        # breakpoint()
        # tool_calls = json.loads(message)
        if isinstance(tool_calls, dict):
            tool_calls = [tool_calls]
            
        for tool_call in tool_calls:
            tool_call["id"] = generator_tool_call_id()
            # if "function" in tool_call:
            
            # else:
            tool_call["name"] = tool_call["name"].replace("__", "/")
            # tool_call["type"] = "function"
        return tool_calls
    
    def pre_process_tools(self, tools):
        for tool in tools:
            tool_name = tool["function"]["name"]
            if "/" in tool_name:
                tool_name = "__".join(tool_name.split("/"))
                tool["function"]["name"] = tool_name
        return tools
    
    def address_syscall(
        self,
        llm_syscall,
        temperature=0.0
    ):
        """
        Address request sent from the agent

        Args:
            llm_syscall (LLMSyscall)      : LLMSyscall object that contains
                                            request sent from the agent
            temperature (float, optional) : Parameter to control the randomness
                                            of LLM output. Defaults to 0.0.
        """
        try:
            messages = llm_syscall.query.messages
            tools = llm_syscall.query.tools
            ret_type = llm_syscall.query.message_return_type
            selected_llms = llm_syscall.query.llms if llm_syscall.query.llms else self.llm_configs

            llm_syscall.set_status("executing")
            llm_syscall.set_start_time(time.time())
            restored_context = None
                
            if self.context_manager:
                pid = llm_syscall.get_pid()
                if self.context_manager.check_restoration(pid):
                    restored_context = self.context_manager.gen_recover(pid)

            if restored_context:
                messages += [{
                    "role": "assistant",
                    "content": "" + restored_context,
                }]

            if tools:
                tools = self.pre_process_tools(tools)
                messages = self.tool_calling_input_format(messages, tools)

            model_idxs = self.strategy.get_model_idxs(selected_llms)
            
            model_idx = model_idxs[0]
            
            model = self.llms[model_idx]

            if isinstance(model, (str, HfLocalBackend, VLLMLocalBackend, OllamaBackend)):
                try:
                    if isinstance(model, str):
                        # Extract content correctly when using litellm completion
                        completion_response = completion(
                            model=model,
                            messages=messages,
                            temperature=temperature,
                        )
                        res = completion_response.choices[0].message.content
                    else:
                        # Directly call the local backend model
                        res = model(
                            messages=messages,
                            temperature=temperature,
                        )
                except Exception as e:
                    error_msg = str(e)
                    # Mask API key in error message - only show first and last 2 chars
                    if "API key provided:" in error_msg:
                        key_start = error_msg.find("API key provided:") + len("API key provided: ")
                        key_end = error_msg.find(".", key_start)
                        if key_end == -1:  # If no period found, find next space
                            key_end = error_msg.find(" ", key_start)
                        if key_end != -1:  # If we found the end of the key
                            api_key = error_msg[key_start:key_end]
                            masked_key = f"{api_key[:2]}****{api_key[-2:]}" if len(api_key) > 4 else "****"
                            error_msg = error_msg[:key_start] + masked_key + error_msg[key_end:]

                    if "Invalid API key" in error_msg or "API key not found" in error_msg:
                        return LLMResponse(
                            response_message="Error: Invalid or missing API key for the selected model.",
                            error=error_msg,
                            finished=True,
                            status_code=402
                        )
                    return LLMResponse(
                        response_message=f"LLM Error: {error_msg}",
                        error=error_msg,
                        finished=True,
                        status_code=500
                    )

            if tools:
                if tool_calls := self.parse_tool_calls(res):
                    return LLMResponse(response_message=None,
                                    tool_calls=tool_calls,
                                    finished=True)

            if ret_type == "json":
                res = self.parse_json_format(res)

            return LLMResponse(response_message=res, finished=True)

        except Exception as e:
            # Handle system level errors
            return LLMResponse(
                response_message=f"System Error: {str(e)}",
                error=str(e),
                finished=True,
                status_code=500
            )
