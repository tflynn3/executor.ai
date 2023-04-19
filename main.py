import json
import logging
import os
import subprocess
from pathlib import Path
import datetime
import openai
import re
from tiktoken import get_encoding
from tiktoken import encoding_for_model

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class Workspace:
    def __init__(self, base_directory="workspace"):
        self.base_directory = Path(base_directory)
        self.timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        self.run_directory = self.base_directory / self.timestamp
        self.output_directory = self.run_directory / "outputs"
        self.run_directory.mkdir(parents=True, exist_ok=True)
        self.output_directory.mkdir(exist_ok=True)

    def save_file(self, filename, content, subdir=None):
        if subdir:
            subdir_path = self.run_directory / subdir
            subdir_path.mkdir(exist_ok=True)
            file_path = subdir_path / filename
        else:
            file_path = self.run_directory / filename

        with open(file_path, "w") as f:
            f.write(content)

class ResultsManager:
    def __init__(self, messages, openai_api_key, workspace):
        self.messages = messages
        self.openai_api_key = openai_api_key
        self.workspace = workspace

    def create_openai_chat_completion(self, messages):
        return openai.ChatCompletion.create(
            model="gpt-4",
            messages=messages
        )

    def summarize_results(self, conversation):
        conversation.append({"role": "system", "content": "Please summarize and document the results in markdown. Additionally, include why you believe the task was completed"})
        response = self.create_openai_chat_completion(conversation)

        assistant_response = response["choices"][0]["message"]['content']
        return assistant_response

    def decide_if_task_completed(self, conversation):
        conversation.append({"role": "system", "content": "Respond either \"Yes\" or \"No\". Would you say the task was completed to the full potential?"})
        response = self.create_openai_chat_completion(conversation)

        assistant_response = response["choices"][0]["message"]['content']
        logger.info(f"Assistant decision on task completion: {assistant_response}")

        return "yes" in assistant_response.lower()

    def save_summary(self, summary):
        self.workspace.save_file("summary.md", summary, subdir="outputs")

class PowerShellExecutor:
    def __init__(self, prompt, results_manager, workspace):
        self.openai_organization = "org-SK9LjCRvUYn33UGLHqAkFEQe"
        self.openai_api_key = os.getenv("OPENAI_API_KEY")
        self.prompt = prompt
        self.messages = [
            {"role": "system", "content": "You respond only with shell commands in the following JSON format {\"command]\": \"Insert your command here\"}."},
            {"role": "user", "content": prompt}
        ]
        self.commands_executed = []
        self.outputs = []
        self.results_manager = results_manager
        self.workspace = workspace
        encoding_for_model("gpt-3.5-turbo")

    def add_message(self, role, content):
        MAX_MESSAGE_LENGTH = 4000
        content_chunks = [content[i:i + MAX_MESSAGE_LENGTH] for i in range(0, len(content), MAX_MESSAGE_LENGTH)]

        for chunk in content_chunks:
            self.messages.append({"role": role, "content": chunk})

    def truncate_messages(self):
        # Calculate total tokens in the current conversation
        total_tokens = sum(len(get_encoding(msg["content"])) for msg in self.messages)

        # Account for the additional tokens added to the request by GPT-3.5-turbo
        additional_tokens = 2 * len(self.messages) + 4
        safety_margin = 10  # Add a safety margin to prevent context length exceeded errors
        max_tokens = 4096 - additional_tokens - safety_margin

        while total_tokens > max_tokens:
            removed_message = self.messages.pop(0)
            removed_tokens = len(get_encoding(removed_message["content"]))
            total_tokens -= removed_tokens
            logger.info(f"Removed a message to fit within the token limit. Remaining tokens: {total_tokens}")

    def extract_json(self, response):

        # try to extract json from the response
        try:
            json_obj = json.loads(response)
            return json_obj["command"]
        except json.JSONDecodeError:
            # Search for JSON objects containing the "command" key
            json_matches = re.findall(r'\{[^}]*"command"[^}]*\}', response)

            # Attempt to parse each JSON object
            commands = []
            for json_string in json_matches:
                try:
                    json_obj = json.loads(json_string)
                    commands.append(json_obj["command"])
                except json.JSONDecodeError:
                    continue

        return commands
    def create_openai_chat_completion(self, messages):
        return openai.ChatCompletion.create(
            model="gpt-4",
            messages=messages
        )

    def execute_command(self, command):
        try:
            output = subprocess.run(["powershell", "-Command", command], capture_output=True, text=True, timeout=60)
            return output
        except subprocess.TimeoutExpired:
            logger.info(f"Command timed out. Skipping.")
            return None

    def run_commands(self):
        errors = 0

        while len(self.commands_executed) < 5 or errors > 3:
            response = self.create_openai_chat_completion(self.messages)
            assistant_response = response["choices"][0]["message"]['content']

            logger.info(f"Assistant response: {assistant_response}")

            if "task completed" in assistant_response.lower():
                break

            # Get commands from the assistant's response
            commands = self.extract_json(assistant_response)
            self.messages.append({"role": "system", "content": assistant_response})
            logger.info(f"Number of commands extracted from assistant response: {len(commands)}")

            if not commands:
                errors += 1
                logger.info(f"Assistant response is not in JSON format. Skipping.")
                self.messages.extend([
                    {"role": "user", "content": self.prompt},
                    {"role": "user", "content": "Please respond with a JSON object in the following format {\"command]\": \"Insert your command here\"}. The response must be JSON parsable with a single key \"command\" and a value of the command to execute. One one command at a time. Do not comment on the JSON. Use the workspace to save files. "}
                ])
                continue

            for command in commands:
                #self.truncate_messages()
                logger.info(f"Executing command: {command}")
                self.commands_executed.append(command)
                output = self.execute_command(command)

                if output is None:
                    self.add_message("user", "The previous command timed out. Please respond with a different command.")
                    self.add_message("user", "Please respond with a JSON object in the following format {\"command]\": \"Insert your command here\"}. The response must be JSON parsable with a single key \"command\" and a value of the command to execute. One one command at a time. Do not comment on the JSON. Use the workspace to save files. ")
                    continue

                if output.stdout:
                    trimmed_stdout = output.stdout[:8000]
                    self.add_message("system", f"Standard Output: {trimmed_stdout}")
                else:
                    self.add_message("system", "No output.")

                if output.stderr:
                    trimmed_stderr = output.stderr[:8000]
                    self.add_message("system", f"Standard Error: {trimmed_stderr}")

                logger.info(f"Output: {output}")
                self.outputs.append({"stdout": output.stdout, "stderr": output.stderr})

                self.add_message("user", "Please respond \"Task completed\" if the output meets the requirements.")

        return self.commands_executed, self.outputs

    def save_commands_and_outputs(self):
            n = len(list(self.workspace.output_directory.glob("*")))
            filename = f"commands_{n}.json"

            commands_and_outputs = list(zip(self.commands_executed, self.outputs))

            self.workspace.save_file(filename, json.dumps(commands_and_outputs), subdir="outputs")

if __name__ == "__main__":
    # Key
    openai_api_key = os.getenv("OPENAI_API_KEY")

    workspace = Workspace()
    results_manager = ResultsManager([], openai_api_key, workspace)
    
    workspace_directory = str(workspace.run_directory.resolve())

    prompt = f"""
    Create a fun website with html, css, and javascript that creatively says hi to Caffrey.
    Add content to each file. The website should be responsive and have a button that says "Say hi to Caffrey" and when clicked, it should say hi to Caffrey.
    Finally, open chrome and navigate to the page. 
    Make sure to run chrome in the background.
    Use PowerShell Cmdlets. Your workspace directory is '{workspace_directory}'. If you want to create files, do so in this directory using absolute paths.
    """

    executor = PowerShellExecutor(prompt, results_manager, workspace)

    logger.info(f"Prompt: {prompt.strip()}")

    executor.run_commands()

    max_runs = 3
    runs = 0
    while not results_manager.decide_if_task_completed(executor.messages) and runs < max_runs:
        logger.info(f"Task not completed. Running again.")
        executor.run_commands()
        runs += 1

    summary = results_manager.summarize_results(executor.messages)
    results_manager.save_summary(summary)
    executor.save_commands_and_outputs()
