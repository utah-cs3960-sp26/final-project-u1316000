const storyDataElement = document.getElementById("player-story-data");
const storyData = JSON.parse(storyDataElement.textContent);

const dialogueText = document.getElementById("dialogue-text");
const speakerLabel = document.getElementById("speaker-label");
const lineProgress = document.getElementById("line-progress");
const continueButton = document.getElementById("continue-button");
const statusText = document.getElementById("status-text");
const choicesPanel = document.getElementById("choices-panel");
const locationPill = document.getElementById("location-pill");
const settingsButton = document.getElementById("settings-button");
const settingsMenu = document.getElementById("settings-menu");
const restartButton = document.getElementById("restart-button");
const backgroundImage = document.getElementById("background-image");
const fallbackBackdrop = document.getElementById("fallback-backdrop");
const actorsLayer = document.getElementById("actors-layer");

const typingDelayMs = 24;

let currentSceneKey = storyData.start_scene;
let currentLineIndex = 0;
let typedCharacterCount = 0;
let typingTimer = null;
let isTyping = false;
let hasReachedChoices = false;

function getCurrentScene() {
    return storyData.scenes[currentSceneKey];
}

function getCurrentLine() {
    const scene = getCurrentScene();
    return scene.lines[currentLineIndex];
}

function setStatus(message) {
    statusText.textContent = message;
}

function stopTyping() {
    if (typingTimer !== null) {
        window.clearInterval(typingTimer);
        typingTimer = null;
    }
    isTyping = false;
}

function revealEntireLine() {
    stopTyping();
    const line = getCurrentLine();
    dialogueText.textContent = line.text;
    setStatus("Press Space or Continue.");
}

function setBackground(scene) {
    if (scene.background_url) {
        backgroundImage.style.backgroundImage = `url("${scene.background_url}")`;
        backgroundImage.classList.remove("hidden");
        fallbackBackdrop.classList.add("hidden");
        return;
    }

    backgroundImage.style.backgroundImage = "";
    backgroundImage.classList.add("hidden");
    fallbackBackdrop.classList.remove("hidden");
}

function createPlayerFallback() {
    const silhouette = document.createElement("div");
    silhouette.className = "player-silhouette";
    silhouette.innerHTML = `
        <span class="tophat"></span>
        <span class="head"></span>
        <span class="body"></span>
    `;
    return silhouette;
}

function shouldHideActor(actor, lineIndex) {
    return Array.isArray(actor.hidden_on_lines) && actor.hidden_on_lines.includes(lineIndex);
}

function renderActors(scene) {
    actorsLayer.innerHTML = "";
    const actors = scene.actors || [];

    actors.forEach((actor) => {
        if (shouldHideActor(actor, currentLineIndex)) {
            return;
        }

        const actorElement = document.createElement("div");
        actorElement.className = `scene-actor slot-${actor.slot}`;
        if (actor.focus) {
            actorElement.classList.add("focus");
        }
        actorElement.style.transform = actor.scale
            ? `translateX(${actor.slot === "hero-center" || actor.slot === "center-foreground-object" ? "-50%" : "0"}) scale(${actor.scale})`
            : "";

        if (actor.asset_url) {
            const image = document.createElement("img");
            image.src = actor.asset_url;
            image.alt = `${actor.entity_type} ${actor.entity_id}`;
            actorElement.appendChild(image);
        } else if (actor.use_player_fallback) {
            actorElement.appendChild(createPlayerFallback());
        } else {
            return;
        }

        actorsLayer.appendChild(actorElement);
    });
}

function renderScenePresentation() {
    const scene = getCurrentScene();
    setBackground(scene);
    renderActors(scene);
}

function typeCurrentLine() {
    stopTyping();
    const scene = getCurrentScene();
    const line = getCurrentLine();

    locationPill.textContent = scene.location || "Unknown";
    speakerLabel.textContent = line.speaker || "Narrator";
    lineProgress.textContent = `${currentLineIndex + 1} / ${scene.lines.length}`;
    dialogueText.textContent = "";
    typedCharacterCount = 0;
    isTyping = true;
    hasReachedChoices = false;
    continueButton.disabled = false;
    continueButton.textContent = "Continue";
    choicesPanel.classList.add("hidden");
    choicesPanel.innerHTML = "";
    setStatus("Press Space to reveal or continue.");
    renderScenePresentation();

    typingTimer = window.setInterval(() => {
        typedCharacterCount += 1;
        dialogueText.textContent = line.text.slice(0, typedCharacterCount);
        if (typedCharacterCount >= line.text.length) {
            stopTyping();
            setStatus("Press Space or Continue.");
        }
    }, typingDelayMs);
}

function renderChoices() {
    const scene = getCurrentScene();
    hasReachedChoices = true;
    choicesPanel.innerHTML = "";
    choicesPanel.classList.remove("hidden");
    continueButton.disabled = true;
    continueButton.textContent = "Choose";
    setStatus(`Choose with click or keys 1-${scene.choices.length}.`);

    scene.choices.forEach((choice, index) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "choice-button";
        button.innerHTML = `
            <span class="choice-number">${index + 1}</span>
            <span class="choice-label">${choice.label}</span>
        `;
        button.addEventListener("click", () => startScene(choice.target));
        choicesPanel.appendChild(button);
    });
}

function finishScene() {
    const scene = getCurrentScene();
    if (scene.choices && scene.choices.length > 0) {
        renderChoices();
        return;
    }

    continueButton.disabled = true;
    continueButton.textContent = "End";
    setStatus("This path pauses here. Restart from settings to replay.");
}

function advanceDialogue() {
    if (isTyping) {
        revealEntireLine();
        return;
    }

    if (hasReachedChoices) {
        return;
    }

    const scene = getCurrentScene();
    if (currentLineIndex < scene.lines.length - 1) {
        currentLineIndex += 1;
        typeCurrentLine();
        return;
    }

    finishScene();
}

function startScene(sceneKey) {
    stopTyping();
    currentSceneKey = sceneKey;
    currentLineIndex = 0;
    typeCurrentLine();
}

function restartAdventure() {
    settingsMenu.classList.add("hidden");
    startScene(storyData.start_scene);
}

continueButton.addEventListener("click", advanceDialogue);
settingsButton.addEventListener("click", () => {
    settingsMenu.classList.toggle("hidden");
});
restartButton.addEventListener("click", restartAdventure);

document.addEventListener("click", (event) => {
    if (
        !settingsMenu.classList.contains("hidden") &&
        !settingsMenu.contains(event.target) &&
        !settingsButton.contains(event.target)
    ) {
        settingsMenu.classList.add("hidden");
    }
});

document.addEventListener("keydown", (event) => {
    if (event.code === "Space") {
        event.preventDefault();
        advanceDialogue();
        return;
    }

    if (!choicesPanel.classList.contains("hidden")) {
        const scene = getCurrentScene();
        if (event.key >= "1" && event.key <= String(Math.min(scene.choices.length, 9))) {
            const index = Number.parseInt(event.key, 10) - 1;
            if (scene.choices[index]) {
                startScene(scene.choices[index].target);
            }
        }
    }
});

startScene(storyData.start_scene);
