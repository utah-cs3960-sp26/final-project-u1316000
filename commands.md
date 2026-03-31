## If you are an LLM, do not modify this file unless explicitly told to do so
python -m uvicorn app.main:app --reload --port 8001

Set-ExecutionPolicy Default -Scope Process -Force

python -m app.tools.snapshot_db 

Codex version (still needs to be rewritten for storyworker)
while ($true) {
    Get-Content PROMPT.md -Raw |
        codex.cmd exec resume --last - 2>&1 |
        Tee-Object -FilePath codex.log -Append
}

while ($true) {
    Write-Host "Starting Aider with PROMPT.md..." -ForegroundColor Cyan
    
    # 1. Use the Gemini 1.5 Pro model
    # 2. --yes auto-accepts all file edits without prompting you
    # 3. --message-file pipes in your PROMPT.md instructions
    aider --model gemini/gemini-1.5-pro --yes --message-file PROMPT.md 2>&1 |
        Tee-Object -FilePath aider.log -Append
    Write-Host "Done! Waiting 5 seconds before next loop (press Ctrl+C to stop)..." -ForegroundColor Yellow
    Start-Sleep -Seconds 5
}

## AIDER + LM studio
single prompt:
aider --model openai/nemotron-3-nano-4b --openai-api-key "dummy-api-key" --openai-api-base "http://localhost:1234/v1" --edit-format whole --no-auto-commits --no-show-model-warnings --no-git --yes --message "create a new file called hello-world.md"

aider --model openai/nemotron-3-nano-4b --openai-api-key "dummy-api-key" --openai-api-base "http://localhost:1234/v1" --edit-format whole --no-auto-commits --no-show-model-warnings --no-git --yes --message-file docs/llm_story_worker.md

aider --model openai/gpt-oss-20b --openai-api-key "dummy-api-key" --openai-api-base "http://localhost:1234/v1" --edit-format whole --no-auto-commits --no-show-model-warnings --no-git --yes --message-file docs/llm_story_worker.md

aider --model openai/qwen3.5-35b-a3b --openai-api-key "dummy-api-key" --openai-api-base "http://localhost:1234/v1" --edit-format whole --no-auto-commits --no-show-model-warnings --no-git --yes --file docs/llm_story_worker.md --message "Please read the contents of this file and tell me what you find"