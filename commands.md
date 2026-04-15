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


### This one below is good
python -m app.tools.run_story_worker_local --max-retries 3 --request-timeout 1800 --model nvidia/nemotron-3-nano-4b 
while ($true) { python -m app.tools.run_story_worker_local --max-retries 3 --request-timeout 1800 --model nvidia/nemotron-3-nano-4b }

python -m app.tools.run_story_worker_local --model qwen/qwen3.5-35b-a3b

lms load qwen/qwen3.5-35b-a3b --identifier story-qwen --ttl 60 --gpu max --context-length 100000 -y && python -m app.tools.run_story_worker_local --model story-qwen && lms unload story-qwen


lms load qwen/qwen3.5-35b-a3b --identifier story-qwen --ttl 60 --gpu max --context-length 100000 -y; try { if ($?) { python -m app.tools.run_story_worker_local --model story-qwen } } finally { lms unload story-qwen }


lms load qwen/qwen3.5-35b-a3b --identifier story-qwen --ttl 60 --gpu max --context-length 100000 -y; try { if ($?) { python -m app.tools.run_story_worker_local --model story-qwen --request-timeout 1800 } } finally { lms unload story-qwen }


Nw ox stuff:
python -m app.tools.run_story_worker_codex_human --codex-model gpt-5.4-mini
python -m app.tools.run_story_worker_codex_human --codex-model gpt-5.4-mini --dry-run --choice-id 73


GOAT:
python -m app.tools.run_story_worker_local --model google/gemma-4-e4b --max-retries 4 --request-timeout 1800 --context-run-limit 1 --reset-context
