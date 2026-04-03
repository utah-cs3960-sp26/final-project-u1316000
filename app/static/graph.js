document.addEventListener("DOMContentLoaded", () => {
    const data = window.__GRAPH_DATA__;
    if (!data || !data.nodes.length) return;

    const canvas = document.getElementById("graph-canvas");
    const ctx = canvas.getContext("2d");
    const popup = document.getElementById("node-popup");

    // --- Constants ---
    const NODE_RADIUS = 45;
    const LEVEL_SPACING = 160;
    const SIBLING_SPACING = 140;
    const BORDER_WIDTH = 3;
    const COLOR_BRANCH = "#9f4f2d";
    const COLOR_LEAF = "#6b6154";
    const ZOOM_MIN = 0.2;
    const ZOOM_MAX = 3.0;
    const FONT_FAMILY = "Georgia, serif";

    // --- State ---
    let offsetX = 0;
    let offsetY = 0;
    let scale = 1;
    let dirty = true;
    let dragging = false;
    let dragStartX = 0;
    let dragStartY = 0;
    let dragOffsetX = 0;
    let dragOffsetY = 0;
    let hoveredNode = null;
    let selectedNode = null;

    // --- Image cache ---
    const imageCache = {};

    function loadNodeImage(url) {
        if (imageCache[url]) return imageCache[url];
        const img = new Image();
        img.crossOrigin = "anonymous";
        img.src = url;
        img.onload = () => { dirty = true; };
        imageCache[url] = img;
        return img;
    }

    // --- Build node map and adjacency ---
    const nodeMap = {};
    data.nodes.forEach(n => { nodeMap[n.id] = { ...n, x: 0, y: 0 }; });

    const childrenMap = {};
    const outgoingEdges = {};
    data.nodes.forEach(n => {
        childrenMap[n.id] = [];
        outgoingEdges[n.id] = [];
    });

    data.nodes.forEach(n => {
        if (n.parent_node_id != null && nodeMap[n.parent_node_id]) {
            childrenMap[n.parent_node_id].push(n.id);
        }
    });

    const parentChildPairs = new Set();
    data.nodes.forEach(n => {
        if (n.parent_node_id != null) {
            parentChildPairs.add(n.parent_node_id + "->" + n.id);
        }
    });

    const treeEdges = [];
    const crossEdges = [];
    data.edges.forEach(e => {
        if (!nodeMap[e.from_node_id] || !nodeMap[e.to_node_id]) return;
        const key = e.from_node_id + "->" + e.to_node_id;
        if (parentChildPairs.has(key)) {
            treeEdges.push(e);
        } else {
            crossEdges.push(e);
        }
        outgoingEdges[e.from_node_id].push(e);
    });

    // --- Tree layout ---
    const roots = data.nodes.filter(n => n.parent_node_id == null || !nodeMap[n.parent_node_id]);

    function assignDepths(nodeId, depth, visited) {
        if (visited.has(nodeId)) return;
        visited.add(nodeId);
        const node = nodeMap[nodeId];
        node.depth = depth;
        childrenMap[nodeId].forEach(cid => assignDepths(cid, depth + 1, visited));
    }

    const visited = new Set();
    roots.forEach(r => assignDepths(r.id, 0, visited));
    // Handle disconnected nodes
    data.nodes.forEach(n => {
        if (!visited.has(n.id)) {
            nodeMap[n.id].depth = 0;
        }
    });

    function computeSubtreeWidth(nodeId) {
        const children = childrenMap[nodeId];
        if (children.length === 0) return SIBLING_SPACING;
        let total = 0;
        children.forEach(cid => { total += computeSubtreeWidth(cid); });
        return Math.max(total, SIBLING_SPACING);
    }

    function layoutSubtree(nodeId, leftX, depth) {
        const node = nodeMap[nodeId];
        const children = childrenMap[nodeId];
        const subtreeWidth = computeSubtreeWidth(nodeId);

        node.x = leftX + subtreeWidth / 2;
        node.y = depth * LEVEL_SPACING + NODE_RADIUS + 40;

        if (children.length > 0) {
            let cx = leftX;
            children.forEach(cid => {
                const cw = computeSubtreeWidth(cid);
                layoutSubtree(cid, cx, depth + 1);
                cx += cw;
            });
        }
    }

    let currentLeft = 0;
    roots.forEach(r => {
        const w = computeSubtreeWidth(r.id);
        layoutSubtree(r.id, currentLeft, 0);
        currentLeft += w + SIBLING_SPACING * 0.5;
    });

    // Place disconnected nodes that aren't in any tree
    let disconnectedX = currentLeft + SIBLING_SPACING;
    data.nodes.forEach(n => {
        if (!visited.has(n.id)) {
            nodeMap[n.id].x = disconnectedX;
            nodeMap[n.id].y = NODE_RADIUS + 40;
            disconnectedX += SIBLING_SPACING;
        }
    });

    // Preload images
    data.nodes.forEach(n => {
        if (n.background_url) loadNodeImage(n.background_url);
    });

    // --- Canvas sizing ---
    function resizeCanvas() {
        const dpr = window.devicePixelRatio || 1;
        const rect = canvas.getBoundingClientRect();
        canvas.width = rect.width * dpr;
        canvas.height = rect.height * dpr;
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        dirty = true;
    }

    resizeCanvas();
    window.addEventListener("resize", resizeCanvas);

    // --- Center view on graph ---
    function centerView() {
        const nodes = Object.values(nodeMap);
        if (nodes.length === 0) return;
        let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
        nodes.forEach(n => {
            minX = Math.min(minX, n.x - NODE_RADIUS);
            maxX = Math.max(maxX, n.x + NODE_RADIUS);
            minY = Math.min(minY, n.y - NODE_RADIUS);
            maxY = Math.max(maxY, n.y + NODE_RADIUS);
        });
        const graphW = maxX - minX;
        const graphH = maxY - minY;
        const rect = canvas.getBoundingClientRect();
        const padding = 80;
        const scaleX = (rect.width - padding * 2) / graphW;
        const scaleY = (rect.height - padding * 2) / graphH;
        scale = Math.min(scaleX, scaleY, 1.5);
        scale = Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, scale));
        const cx = (minX + maxX) / 2;
        const cy = (minY + maxY) / 2;
        offsetX = rect.width / 2 - cx * scale;
        offsetY = rect.height / 2 - cy * scale;
        dirty = true;
    }

    centerView();

    // --- Coordinate transforms ---
    function screenToWorld(sx, sy) {
        return { x: (sx - offsetX) / scale, y: (sy - offsetY) / scale };
    }

    function worldToScreen(wx, wy) {
        return { x: wx * scale + offsetX, y: wy * scale + offsetY };
    }

    // --- Hit detection ---
    function nodeAt(wx, wy) {
        for (let i = data.nodes.length - 1; i >= 0; i--) {
            const n = nodeMap[data.nodes[i].id];
            const dx = n.x - wx;
            const dy = n.y - wy;
            if (dx * dx + dy * dy <= NODE_RADIUS * NODE_RADIUS) return n;
        }
        return null;
    }

    // --- Text helpers ---
    function truncate(str, maxLen) {
        if (!str) return "";
        return str.length > maxLen ? str.slice(0, maxLen - 1) + "\u2026" : str;
    }

    // --- Drawing ---
    function drawEdgeLabel(x, y, text) {
        ctx.save();
        ctx.font = "11px " + FONT_FAMILY;
        const label = truncate(text, 25);
        const metrics = ctx.measureText(label);
        const pw = metrics.width + 10;
        const ph = 16;
        ctx.fillStyle = "rgba(255, 250, 240, 0.85)";
        ctx.beginPath();
        ctx.roundRect(x - pw / 2, y - ph / 2, pw, ph, 6);
        ctx.fill();
        ctx.fillStyle = "#5a4a3a";
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillText(label, x, y);
        ctx.restore();
    }

    function drawTreeEdge(edge) {
        const from = nodeMap[edge.from_node_id];
        const to = nodeMap[edge.to_node_id];
        const x1 = from.x;
        const y1 = from.y + NODE_RADIUS;
        const x2 = to.x;
        const y2 = to.y - NODE_RADIUS;

        ctx.save();
        ctx.strokeStyle = "rgba(159, 79, 45, 0.5)";
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.moveTo(x1, y1);
        ctx.lineTo(x2, y2);
        ctx.stroke();
        ctx.restore();

        if (edge.choice_text) {
            const mx = (x1 + x2) / 2;
            const my = (y1 + y2) / 2;
            drawEdgeLabel(mx, my, edge.choice_text);
        }
    }

    function drawCrossEdge(edge) {
        const from = nodeMap[edge.from_node_id];
        const to = nodeMap[edge.to_node_id];
        const x1 = from.x;
        const y1 = from.y + NODE_RADIUS;
        const x2 = to.x;
        const y2 = to.y - NODE_RADIUS;

        const cpOffset = Math.abs(x2 - x1) * 0.3 + 40;

        ctx.save();
        ctx.strokeStyle = "rgba(107, 97, 84, 0.4)";
        ctx.lineWidth = 2;
        ctx.setLineDash([6, 4]);
        ctx.beginPath();
        ctx.moveTo(x1, y1);
        ctx.bezierCurveTo(x1 + cpOffset, y1 + cpOffset, x2 - cpOffset, y2 - cpOffset, x2, y2);
        ctx.stroke();
        ctx.restore();

        if (edge.choice_text) {
            // Approximate midpoint of bezier
            const t = 0.5;
            const mt = 1 - t;
            const mx = mt * mt * mt * x1 + 3 * mt * mt * t * (x1 + cpOffset) + 3 * mt * t * t * (x2 - cpOffset) + t * t * t * x2;
            const my = mt * mt * mt * y1 + 3 * mt * mt * t * (y1 + cpOffset) + 3 * mt * t * t * (y2 - cpOffset) + t * t * t * y2;
            drawEdgeLabel(mx, my, edge.choice_text);
        }
    }

    function drawNode(node) {
        const n = nodeMap[node.id];
        const isLeaf = outgoingEdges[n.id].length === 0;
        const borderColor = isLeaf ? COLOR_LEAF : COLOR_BRANCH;

        ctx.save();

        // Draw background image or fill
        ctx.beginPath();
        ctx.arc(n.x, n.y, NODE_RADIUS, 0, Math.PI * 2);
        ctx.closePath();
        ctx.save();
        ctx.clip();

        if (n.background_url && imageCache[n.background_url] && imageCache[n.background_url].complete && imageCache[n.background_url].naturalWidth > 0) {
            const img = imageCache[n.background_url];
            const size = NODE_RADIUS * 2;
            ctx.drawImage(img, n.x - NODE_RADIUS, n.y - NODE_RADIUS, size, size);
        } else {
            ctx.fillStyle = "#faf3e8";
            ctx.fill();
        }
        ctx.restore();

        // Border
        ctx.beginPath();
        ctx.arc(n.x, n.y, NODE_RADIUS, 0, Math.PI * 2);
        ctx.strokeStyle = borderColor;
        ctx.lineWidth = BORDER_WIDTH;
        ctx.stroke();

        // Hover highlight
        if (hoveredNode && hoveredNode.id === n.id) {
            ctx.beginPath();
            ctx.arc(n.x, n.y, NODE_RADIUS + 3, 0, Math.PI * 2);
            ctx.strokeStyle = "rgba(159, 79, 45, 0.35)";
            ctx.lineWidth = 2;
            ctx.stroke();
        }

        // Title text
        ctx.fillStyle = "#3a2e22";
        ctx.font = "bold 12px " + FONT_FAMILY;
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        const title = truncate(n.title, 18);
        // If there's a background image, add a text shadow for readability
        if (n.background_url && imageCache[n.background_url] && imageCache[n.background_url].complete) {
            ctx.save();
            // Draw text background pill
            const tm = ctx.measureText(title);
            const pw = tm.width + 8;
            const ph = 16;
            ctx.fillStyle = "rgba(255, 250, 240, 0.8)";
            ctx.beginPath();
            ctx.roundRect(n.x - pw / 2, n.y - ph / 2, pw, ph, 4);
            ctx.fill();
            ctx.fillStyle = "#3a2e22";
            ctx.fillText(title, n.x, n.y);
            ctx.restore();
        } else {
            ctx.fillText(title, n.x, n.y);
        }

        // ID below circle
        ctx.fillStyle = "#7a6a5a";
        ctx.font = "10px " + FONT_FAMILY;
        ctx.fillText("#" + n.id, n.x, n.y + NODE_RADIUS + 14);

        ctx.restore();
    }

    function draw() {
        const rect = canvas.getBoundingClientRect();
        ctx.clearRect(0, 0, rect.width, rect.height);

        ctx.save();
        ctx.translate(offsetX, offsetY);
        ctx.scale(scale, scale);

        // Draw edges first
        treeEdges.forEach(drawTreeEdge);
        crossEdges.forEach(drawCrossEdge);

        // Draw nodes on top
        data.nodes.forEach(drawNode);

        ctx.restore();
    }

    // --- Animation loop ---
    function loop() {
        if (dirty) {
            dirty = false;
            draw();
        }
        requestAnimationFrame(loop);
    }
    requestAnimationFrame(loop);

    // --- Popup ---
    function showPopup(node, screenX, screenY) {
        selectedNode = node;
        const titleEl = popup.querySelector(".popup-title");
        const idEl = popup.querySelector(".popup-id");
        const summaryEl = popup.querySelector(".popup-summary");
        const playLink = popup.querySelector(".popup-play-link");
        const detailsLink = popup.querySelector(".popup-details-link");

        if (titleEl) titleEl.textContent = node.title;
        if (idEl) idEl.textContent = "Node #" + node.id;
        if (summaryEl) summaryEl.textContent = node.summary || "No summary available.";
        if (playLink) playLink.href = "/play?branch_key=" + encodeURIComponent(node.branch_key) + "&scene=" + node.id;
        if (detailsLink) detailsLink.href = "/ui/story";

        const rect = canvas.getBoundingClientRect();
        let px = screenX + 15;
        let py = screenY - 10;

        // Keep popup within viewport
        popup.style.display = "block";
        const popupRect = popup.getBoundingClientRect();
        if (px + popupRect.width > rect.right) px = screenX - popupRect.width - 15;
        if (py + popupRect.height > rect.bottom) py = rect.bottom - popupRect.height - 10;
        if (py < rect.top) py = rect.top + 10;

        popup.style.left = px + "px";
        popup.style.top = py + "px";
    }

    function hidePopup() {
        selectedNode = null;
        if (popup) popup.style.display = "none";
    }

    // --- Mouse events ---
    canvas.addEventListener("mousedown", (e) => {
        const rect = canvas.getBoundingClientRect();
        const mx = e.clientX - rect.left;
        const my = e.clientY - rect.top;
        const world = screenToWorld(mx, my);
        const hit = nodeAt(world.x, world.y);

        if (hit) {
            const sp = worldToScreen(hit.x, hit.y);
            showPopup(hit, sp.x + rect.left, sp.y + rect.top);
        } else {
            hidePopup();
            dragging = true;
            dragStartX = e.clientX;
            dragStartY = e.clientY;
            dragOffsetX = offsetX;
            dragOffsetY = offsetY;
        }
    });

    canvas.addEventListener("mousemove", (e) => {
        const rect = canvas.getBoundingClientRect();
        const mx = e.clientX - rect.left;
        const my = e.clientY - rect.top;

        if (dragging) {
            offsetX = dragOffsetX + (e.clientX - dragStartX);
            offsetY = dragOffsetY + (e.clientY - dragStartY);
            dirty = true;
            return;
        }

        const world = screenToWorld(mx, my);
        const hit = nodeAt(world.x, world.y);
        const prev = hoveredNode;
        hoveredNode = hit;
        canvas.style.cursor = hit ? "pointer" : "grab";
        if (prev !== hoveredNode) dirty = true;
    });

    canvas.addEventListener("mouseup", () => {
        dragging = false;
    });

    canvas.addEventListener("mouseleave", () => {
        dragging = false;
        if (hoveredNode) {
            hoveredNode = null;
            dirty = true;
        }
    });

    canvas.addEventListener("wheel", (e) => {
        e.preventDefault();
        const rect = canvas.getBoundingClientRect();
        const mx = e.clientX - rect.left;
        const my = e.clientY - rect.top;

        const zoomFactor = e.deltaY < 0 ? 1.1 : 1 / 1.1;
        const newScale = Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, scale * zoomFactor));

        // Zoom toward cursor
        offsetX = mx - (mx - offsetX) * (newScale / scale);
        offsetY = my - (my - offsetY) * (newScale / scale);
        scale = newScale;
        dirty = true;
    }, { passive: false });
});
