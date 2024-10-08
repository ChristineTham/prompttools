# Copyright (c) Hegel AI, Inc.
# All rights reserved.
#
# This source code's license can be found in the
# LICENSE file in the root directory of this source tree.
import copy
import os
import json
import pickle
from typing import Dict, List, Optional, Union
import openai
import ollama
import requests
import itertools
import logging

from prompttools.selector.prompt_selector import PromptSelector
from prompttools.mock.mock import mock_openai_chat_completion_fn, mock_openai_chat_function_completion_fn
from .experiment import Experiment
from .error import PromptExperimentException
import pandas as pd
from prompttools.common import HEGEL_BACKEND_URL

class OllamaModels():
    @classmethod
    def list(cls):
        return tuple(m["name"] for m in ollama.list()["models"])
    
class OllamaChatExperiment(Experiment):
    r"""
    This class defines an experiment for OpenAI's chat completion API.
    It accepts lists for each argument passed into OpenAI's API, then creates
    a cartesian product of those arguments, and gets results for each.

    Note:
        - All arguments here should be a ``list``, even if you want to keep the argument frozen
          (i.e. ``temperature=[1.0]``), because the experiment will try all possible combination
          of the input arguments.
        - For detailed description of the input arguments, please reference at OpenAI's chat completion API.

    Args:
        model (list[str]): list of ID(s) of the model(s) to use, e.g. ``["gpt-3.5-turbo", "ft:gpt-3.5-turbo:org_id"]``
            If you are using Azure OpenAI service, put the models' deployment names here

        messages (list[dict]): A list of messages comprising the conversation so far. Each message is represented as a
            dictionary with the following keys: ``role: str``, ``content: str``.

        temperature (list[float]):
            Defaults to [1.0]. What sampling temperature to use, between 0 and 2. Higher values like 0.8 will make
            the output more random, while lower values like 0.2 will make it more focused and deterministic.

        top_p (list[float]):
            Defaults to [1.0]. An alternative to sampling with temperature, called nucleus sampling, where the
            model considers the results of the tokens with top_p probability mass. So 0.1 means only the tokens
            comprising the top 10% probability mass are considered.

        n (list[int]):
            Defaults to [1]. How many chat completion choices to generate for each input message.

        stream (list[bool]):
            Defaults to [False]. If set, partial message deltas will be sent, like in ChatGPT. Tokens will be sent
            as data-only server-sent events as they become available, with the stream terminated by a data: [DONE]
            message.

        stop (list[list[str]]):
            Defaults to [None]. Up to 4 sequences where the API will stop generating further tokens.

        max_tokens (list[int]):
            Defaults to [inf]. The maximum number of tokens to generate in the chat completion.

        presence_penalty (list[float]):
            Defaults to [0.0]. Number between -2.0 and 2.0. Positive values penalize new tokens based on whether
            they appear in the text so far, increasing the model's likelihood to talk about new topics.

        frequency_penalty (list[float]):
            Defaults to [0.0]. Number between -2.0 and 2.0. Positive values penalize new tokens based on their
            existing frequency in the text so far, decreasing the model's likelihood to repeat the same line
            verbatim.

        logit_bias (list[dict]):
            Defaults to [None]. Modify the likelihood of specified tokens appearing in the completion. Accepts a
            json object that maps tokens (specified by their token ID in the tokenizer) to an associated bias value
            from -100 to 100.

        functions (list[dict]):
            Defaults to [None]. A list of dictionaries, each of which contains the definition of a function
            the model may generate JSON inputs for.

        function_call (list[dict]):
            Defaults to [None]. A dictionary containing the name and arguments of a function that should be called,
            s generated by the model.

        response_format (list[Optional[dict]]):
            Setting to `{ type: "json_object" }` enables JSON mode, which guarantees the message
            the model generates is valid JSON.

        seed (list[Optional[int]]):
            This feature is in Beta. If specified, our system will make a best effort to sample deterministically,
            such that repeated requests with the same `seed` and parameters should return the same result.
            Determinism is not guaranteed, and you should refer to the `system_fingerprint` response parameter to
            monitor changes in the backend.

        azure_openai_service_configs (Optional[dict]):
            Defaults to ``None``. If it is set, the experiment will use Azure OpenAI Service. The input dict should
            contain these 2 keys (but with values based on your use case and configuration):
            ``{"AZURE_OPENAI_ENDPOINT": "https://YOUR_RESOURCE_NAME.openai.azure.com/", "API_VERSION": "2023-05-15"}``
    """

    _experiment_type = "RawExperiment"

    def __init__(
        self,
        model: List[str] = ["llama3.1"],
        messages: Union[List[List[Dict[str, str]]], List[PromptSelector]] = [],
        temperature: Optional[List[float]] = [1.0],
        top_p: Optional[List[float]] = [1.0],
        n: Optional[List[int]] = [1],
        stream: Optional[List[bool]] = [False],
        stop: Optional[List[List[str]]] = [None],
        max_tokens: Optional[List[int]] = [float("inf")],
        presence_penalty: Optional[List[float]] = [0.0],
        frequency_penalty: Optional[List[float]] = [0.0],
        logit_bias: Optional[List[Dict]] = [None],
        response_format: List[Optional[Dict]] = [None],
        seed: List[Optional[int]] = [None],
        functions: Optional[List[Dict]] = [None],
        function_call: Optional[List[Dict[str, str]]] = [None],
    ):
        client = openai.OpenAI(
            base_url='http://localhost:11434/v1',
            api_key='ollama', # required, but unused
        )
        self.completion_fn = client.chat.completions.create
        if os.getenv("DEBUG", default=False):
            if functions[0] is not None:
                self.completion_fn = mock_openai_chat_function_completion_fn
            else:
                self.completion_fn = mock_openai_chat_completion_fn

        # If we are using a prompt selector, we need to render
        # messages, as well as create prompt_keys to map the messages
        # to corresponding prompts in other models.
        if len(messages) > 0 and isinstance(messages[0], PromptSelector):
            self.prompt_keys = {
                str(selector.for_openai_chat()[-1]["content"]): selector.for_llama() for selector in messages
            }
            messages = [selector.for_openai_chat() for selector in messages]
        else:
            self.prompt_keys = messages

        self.all_args = dict(
            model=model,
            messages=messages,
            temperature=temperature,
            functions=functions,
            function_call=function_call,
            top_p=top_p,
            n=n,
            stream=stream,
            stop=stop,
            max_tokens=max_tokens,
            presence_penalty=presence_penalty,
            frequency_penalty=frequency_penalty,
            logit_bias=logit_bias,
            seed=seed,
            response_format=response_format,
        )

        # These parameters aren't supported by `gpt-35-turbo`, we can remove them if they are equal to defaults
        # This has no impact on the default case
        if self.all_args["logit_bias"] == [None]:
            del self.all_args["logit_bias"]

        super().__init__()

    @staticmethod
    def _extract_responses(output: openai.types.Completion) -> str:
        message = output.choices[0].message
        if hasattr(message, "function_call") and message.function_call is not None:
            return json.dumps(json.loads(message.function_call.arguments))
        else:
            return message.content

    @staticmethod
    def _is_chat():
        return True

    def _get_model_names(self):
        return [combo["model"] for combo in self.argument_combos]

    def _get_prompts(self):
        return [self.prompt_keys[str(combo["messages"][-1]["content"])] for combo in self.argument_combos]

    def _get_state(self):
        partial_col_names = self.partial_df.columns.tolist()
        score_col_names = self.score_df.columns.tolist()
        state_params = {
            "prompt_keys": self.prompt_keys,
            "all_args": self.all_args,
            "partial_col_names": partial_col_names,
            "score_col_names": score_col_names,
        }
        state = (
            state_params,
            self.full_df,
        )
        print("Creating state of experiment...")
        return state

    def save_experiment(self, name: Optional[str] = None):
        r"""
        name (str, optional): Name of the experiment. This is optional if you have previously loaded an experiment
            into this object.
        """
        if name is None and self._experiment_id is None:
            raise RuntimeError("Please provide a name for your experiment.")
        if self.full_df is None:
            raise RuntimeError("Cannot save empty experiment. Please run it first.")
        if os.environ["HEGELAI_API_KEY"] is None:
            raise PermissionError("Please set HEGELAI_API_KEY (e.g. os.environ['HEGELAI_API_KEY']).")
        state = self._get_state()
        url = f"{HEGEL_BACKEND_URL}/sdk/save"
        headers = {
            "Content-Type": "application/octet-stream",  # Use a binary content type for pickled data
            "Authorization": os.environ["HEGELAI_API_KEY"],
        }
        print("Sending HTTP POST request...")
        data = pickle.dumps((name, self._experiment_id, self._experiment_type, state))
        response = requests.post(url, data=data, headers=headers)
        self._experiment_id = response.json().get("experiment_id")
        self._revision_id = response.json().get("revision_id")
        return response

    @classmethod
    def load_experiment(cls, experiment_id: str):
        r"""
        experiment_id (str): experiment ID of the experiment that you wish to load.
        """
        if os.environ["HEGELAI_API_KEY"] is None:
            raise PermissionError("Please set HEGELAI_API_KEY (e.g. os.environ['HEGELAI_API_KEY']).")

        url = f"{HEGEL_BACKEND_URL}/sdk/get/experiment/{experiment_id}"
        headers = {
            "Content-Type": "application/octet-stream",  # Use a binary content type for pickled data
            "Authorization": os.environ["HEGELAI_API_KEY"],
        }
        print("Sending HTTP GET request...")
        response = requests.get(url, headers=headers)
        if response.status_code == 200:  # Note that state should not have `name` included
            new_experiment_id, revision_id, experiment_type_str, state = pickle.loads(response.content)
            if new_experiment_id != experiment_id:
                raise RuntimeError("Experiment ID mismatch between request and response.")
            return cls._load_state(state, experiment_id, revision_id, experiment_type_str)
        else:
            print(f"Error: {response.status_code}, {response.text}")

    @classmethod
    def load_revision(cls, revision_id: str):
        r"""
        revision_id (str): revision ID of the experiment that you wish to load.
        """
        if os.environ["HEGELAI_API_KEY"] is None:
            raise PermissionError("Please set HEGELAI_API_KEY (e.g. os.environ['HEGELAI_API_KEY']).")

        url = f"{HEGEL_BACKEND_URL}/sdk/get/revision/{revision_id}"
        headers = {
            "Content-Type": "application/octet-stream",  # Use a binary content type for pickled data
            "Authorization": os.environ["HEGELAI_API_KEY"],
        }
        print("Sending HTTP GET request...")
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            experiment_id, new_revision_id, experiment_type_str, state = pickle.loads(response.content)
            if new_revision_id != revision_id:
                raise RuntimeError("Revision ID mismatch between request and response.")
            return cls._load_state(state, experiment_id, revision_id, experiment_type_str)
        else:
            print(f"Error: {response.status_code}, {response.text}")

    @classmethod
    def _load_state(cls, state, experiment_id: str, revision_id: str, experiment_type_str: str):
        (
            state_params,
            full_df,
        ) = state
        if experiment_type_str != cls._experiment_type:
            raise RuntimeError(
                f"The Experiment Type you are trying to load is {experiment_type_str},"
                "which does not match the current class."
            )

        all_args, prompt_keys = state_params["all_args"], state_params["prompt_keys"]
        experiment = cls(all_args["model"], all_args["messages"])
        experiment.prompt_keys = prompt_keys
        experiment.all_args = all_args
        experiment.full_df = pd.DataFrame(full_df)
        experiment.partial_df = (
            experiment.full_df[state_params["partial_col_names"]].copy() if experiment.full_df is not None else None
        )
        experiment.score_df = (
            experiment.full_df[state_params["score_col_names"]].copy() if experiment.full_df is not None else None
        )
        experiment._experiment_id = experiment_id
        experiment._revision_id = revision_id
        print("Loaded experiment.")
        return experiment

    def _validate_arg_key(self, arg_name: str) -> None:
        import inspect

        signature = inspect.signature(self.__init__)
        name_exceptions = {"azure_openai_service_configs"}

        if arg_name in [param.name for param in signature.parameters.values()] and arg_name not in name_exceptions:
            return
        else:
            raise RuntimeError("Provided argument name does not match known argument names.")

    def run_partial(self, **kwargs):
        r"""
        Run experiment with against one parameter, which can be existing or new. The new result will
        be appended to any existing DataFrames.

        If the argument value did not exist before, it will be added to the list of argument combinations
        that will be executed in the next run.

        e.g. `experiement.run_partial({model: 'gpt-4'})`
        """
        print("Running partial experiment...")
        if len(kwargs) > 1:
            raise RuntimeError("Not supported.")
        arg_name, arg_value = list(kwargs.items())[0]

        orginal_arg_value = arg_value
        if arg_name == "messages" and isinstance(arg_value, PromptSelector):
            arg_value = arg_value.for_openai_chat()

        partial_all_args = copy.deepcopy(self.all_args)
        partial_all_args[arg_name] = [arg_value]

        partial_argument_combos = [
            dict(zip(partial_all_args, val)) for val in itertools.product(*partial_all_args.values())
        ]
        original_n_results = len(self.queue.get_results()) if self.queue else 0

        # Execute partial experiment
        for combo in partial_argument_combos:
            self.queue.enqueue(
                self.completion_fn,
                # We need to filter out defaults that are invalid JSON from the request
                {k: v for k, v in combo.items() if (v is not None) and (v != float("inf"))},
            )

        # Verify new results are added
        if len(self.queue.get_results()) - original_n_results == 0:
            logging.error("No results. Something went wrong.")
            raise PromptExperimentException

        # Currently, it always append new rows to the results.
        # In the future, we may want to replace existing rows instead.
        self._construct_result_dfs(self.queue.get_input_args(), self.queue.get_results(), self.queue.get_latencies())

        # If `arg_value` didn't exist before, add to `argument_combos`, which will be used in the next `.run()`
        if arg_value not in self.all_args[arg_name]:
            if arg_name == "messages":
                if isinstance(orginal_arg_value, PromptSelector):
                    self.prompt_keys[
                        str(orginal_arg_value.for_openai_chat()[-1]["content"])
                    ] = orginal_arg_value.for_llama()
                else:
                    self.prompt_keys.append(arg_value)
            self.all_args[arg_name].append(arg_value)
            self.prepare()

    def run_one(
        self,
        model: str,
        messages: Union[List[Dict[str, str]], PromptSelector],
        temperature: Optional[float] = 1.0,
        top_p: Optional[float] = 1.0,
        n: Optional[int] = 1,
        stream: Optional[bool] = False,
        stop: Optional[List[str]] = None,
        max_tokens: Optional[int] = float("inf"),
        presence_penalty: Optional[float] = 0.0,
        frequency_penalty: Optional[float] = 0.0,
        logit_bias: Optional[Dict] = None,
        response_format: Optional[dict] = None,
        seed: Optional[int] = None,
        functions: Optional[Dict] = None,
        function_call: Optional[Dict[str, str]] = None,
    ):
        r"""
        Execute one particular configuration of the experiment and add that to the result DataFrame.

        Unlike `run_partial`, this doesn't change the argument combination of the experiment.
        """
        kwargs = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "top_p": top_p,
            "n": n,
            "stream": stream,
            "stop": stop,
            "max_tokens": max_tokens,
            "presence_penalty": presence_penalty,
            "frequency_penalty": frequency_penalty,
            "logit_bias": logit_bias,
            "response_format": response_format,
            "seed": seed,
            "functions": functions,
            "function_call": function_call,
        }
        kwargs = {k: v for k, v in kwargs.items() if (v is not None) and (v != float("inf"))}

        original_n_results = len(self.queue.get_results()) if self.queue else 0
        self.queue.enqueue(
            self.completion_fn,
            kwargs,
        )
        if len(self.queue.get_results()) - original_n_results != 1:
            print(original_n_results)
            print(len(self.queue.get_results()))
            logging.error("No results. Something went wrong.")
            raise PromptExperimentException

        self._construct_result_dfs(self.queue.get_input_args(), self.queue.get_results(), self.queue.get_latencies())

    def get_table(self, get_all_cols: bool = False) -> pd.DataFrame:
        columns_to_hide = [
            "stream",
            "response_id",
            "response_choices",
            "response_created",
            "response_created",
            "response_object",
            "response_model",
            "response_system_fingerprint",
            "revision_id",
            "log_id",
        ]

        if get_all_cols:
            return self.full_df
        else:
            table = self.full_df
            columns_to_hide.extend(
                [
                    col
                    for col in ["temperature", "top_p", "n", "presence_penalty", "frequency_penalty"]
                    if col not in self.partial_df.columns
                ]
            )
            for col in columns_to_hide:
                if col in table.columns:
                    table = table.drop(col, axis=1)
            return table

    # def _update_values_in_dataframe(self):
    #     r"""
    #     If, in the future, we wish to update existing values rather than appending to the end of the row.
    #
    #     # Consider doing a merge left here
    #     #       1. Identify what input_args columns exist
    #     #       2. Use those columns names for pandas to do a merge left
    #     #       3. If a value (from evals mostly) doesn't exist in the new one, put as NaN or empty
    #     #       4. If 1 has the key combo but 2 doesn't, mkae sure to keep the one from 1
    #     #       5. Make sure `scores_df` is correct
    #     # Alternatively, find the index and overwrite those DataFrame rows, where each row is a `pd.Series`.
    #     """
    #     pass
