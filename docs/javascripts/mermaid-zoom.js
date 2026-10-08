/**
 * Mermaid Lightbox & Interactive Zoom
 * Enables click-to-expand with smooth pan-and-zoom for all Mermaid diagrams.
 *
 * NOTE: Material for MkDocs renders Mermaid diagrams inside a shadow DOM
 * (r.attachShadow({mode: "closed"})). We intercept attachShadow to force
 * mode: "open" so the rendered SVG is accessible and interactive.
 */
(function () {
  // Store shadow roots for all mermaid containers
  const shadowRoots = new WeakMap();

  // Patch attachShadow early to ensure shadow roots are open and accessible
  const origAttachShadow = Element.prototype.attachShadow;
  Element.prototype.attachShadow = function (init) {
    const root = origAttachShadow.call(this, { ...init, mode: "open" });
    shadowRoots.set(this, root);
    this._mermaidShadowRoot = root;

    // Inject zoom cursor style into the shadow root
    try {
      const style = document.createElement("style");
      style.textContent = `
        :host {
          cursor: zoom-in !important;
          display: flex !important;
          justify-content: center !important;
          align-items: center !important;
          text-align: center !important;
          margin-left: auto !important;
          margin-right: auto !important;
        }
        svg {
          cursor: zoom-in !important;
          display: block !important;
          margin-left: auto !important;
          margin-right: auto !important;
        }
        * { cursor: zoom-in !important; }
      `;
      root.appendChild(style);
    } catch (_) {}

    return root;
  };

  let lightbox = null;
  let viewport = null;
  let content = null;

  let scale = 1;
  let translateX = 0;
  let translateY = 0;
  let isDragging = false;
  let startX = 0;
  let startY = 0;
  let didDrag = false;

  function createLightbox() {
    if (lightbox) return;

    lightbox = document.createElement("div");
    lightbox.className = "mermaid-lightbox";
    lightbox.id = "mermaid-lightbox";
    lightbox.innerHTML = `
      <div class="mermaid-lightbox-backdrop"></div>
      <div class="mermaid-lightbox-toolbar">
        <button type="button" class="mermaid-lightbox-btn" id="ml-zoom-in" title="Zoom in (+)">
          <svg viewBox="0 0 24 24" width="18" height="18"><path fill="currentColor" d="M15.5 14h-.79l-.28-.27A6.471 6.471 0 0 0 16 9.5 6.5 6.5 0 1 0 9.5 16c1.61 0 3.09-.59 4.23-1.57l.27.28v.79l5 4.99L20.49 19l-4.99-5zm-6 0C7.01 14 5 11.99 5 9.5S7.01 5 9.5 5 14 7.01 14 9.5 11.99 14 9.5 14zM10 7H9v2H7v1h2v2h1v-2h2V9h-2V7z"/></svg>
        </button>
        <button type="button" class="mermaid-lightbox-btn" id="ml-zoom-out" title="Zoom out (-)">
          <svg viewBox="0 0 24 24" width="18" height="18"><path fill="currentColor" d="M15.5 14h-.79l-.28-.27A6.471 6.471 0 0 0 16 9.5 6.5 6.5 0 1 0 9.5 16c1.61 0 3.09-.59 4.23-1.57l.27.28v.79l5 4.99L20.49 19l-4.99-5zm-6 0C7.01 14 5 11.99 5 9.5S7.01 5 9.5 5 14 7.01 14 9.5 11.99 14 9.5 14zM7 9h5v1H7V9z"/></svg>
        </button>
        <button type="button" class="mermaid-lightbox-btn" id="ml-reset" title="Reset zoom (0)">
          <svg viewBox="0 0 24 24" width="18" height="18"><path fill="currentColor" d="M12 5V1L7 6l5 5V7c3.31 0 6 2.69 6 6s-2.69 6-6 6-6-2.69-6-6H4c0 4.42 3.58 8 8 8s8-3.58 8-8-3.58-8-8-8z"/></svg>
        </button>
        <button type="button" class="mermaid-lightbox-btn ml-close" id="ml-close" title="Close (Esc)">
          <svg viewBox="0 0 24 24" width="18" height="18"><path fill="currentColor" d="M19 6.41L17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12 19 6.41z"/></svg>
        </button>
      </div>
      <div class="mermaid-lightbox-hint">Scroll wheel to zoom · Drag to pan · Click outside or Esc to close</div>
      <div class="mermaid-lightbox-viewport" id="ml-viewport">
        <div class="mermaid-lightbox-content mermaid" id="ml-content"></div>
      </div>
    `;

    document.body.appendChild(lightbox);

    viewport = document.getElementById("ml-viewport");
    content = document.getElementById("ml-content");

    // Toolbar buttons
    document.getElementById("ml-zoom-in").addEventListener("click", (e) => {
      e.stopPropagation();
      applyZoom(1.3);
    });
    document.getElementById("ml-zoom-out").addEventListener("click", (e) => {
      e.stopPropagation();
      applyZoom(0.77);
    });
    document.getElementById("ml-reset").addEventListener("click", (e) => {
      e.stopPropagation();
      resetTransform();
    });
    document.getElementById("ml-close").addEventListener("click", (e) => {
      e.stopPropagation();
      closeLightbox();
    });

    // Backdrop click
    lightbox.querySelector(".mermaid-lightbox-backdrop").addEventListener("click", closeLightbox);

    // Viewport mouse interactions (wheel zoom + drag pan)
    viewport.addEventListener("wheel", onWheel, { passive: false });
    viewport.addEventListener("mousedown", onMouseDown);
    window.addEventListener("mousemove", onMouseMove);
    window.addEventListener("mouseup", onMouseUp);

    // Double-click to toggle zoom
    viewport.addEventListener("dblclick", (e) => {
      if (e.target.closest(".mermaid-lightbox-toolbar")) return;
      if (scale > 1.2) {
        resetTransform();
      } else {
        applyZoom(1.8);
      }
    });

    // Keyboard shortcuts
    window.addEventListener("keydown", (e) => {
      if (!lightbox || !lightbox.classList.contains("is-open")) return;
      if (e.key === "Escape") {
        closeLightbox();
      } else if (e.key === "+" || e.key === "=") {
        applyZoom(1.25);
      } else if (e.key === "-" || e.key === "_") {
        applyZoom(0.8);
      } else if (e.key === "0") {
        resetTransform();
      }
    });
  }

  function updateTransform() {
    if (!content) return;
    content.style.transform = `translate(${translateX}px, ${translateY}px) scale(${scale})`;
  }

  function applyZoom(factor) {
    const newScale = Math.min(Math.max(scale * factor, 0.4), 8.0);
    scale = newScale;
    updateTransform();
  }

  function resetTransform() {
    scale = 1;
    translateX = 0;
    translateY = 0;
    updateTransform();
  }

  function onWheel(e) {
    e.preventDefault();
    const factor = e.deltaY < 0 ? 1.15 : 0.87;
    applyZoom(factor);
  }

  function onMouseDown(e) {
    if (e.target.closest(".mermaid-lightbox-toolbar")) return;
    isDragging = true;
    didDrag = false;
    startX = e.clientX - translateX;
    startY = e.clientY - translateY;
    viewport.style.cursor = "grabbing";
  }

  function onMouseMove(e) {
    if (!isDragging) return;
    const newX = e.clientX - startX;
    const newY = e.clientY - startY;
    if (Math.abs(newX - translateX) > 3 || Math.abs(newY - translateY) > 3) {
      didDrag = true;
    }
    translateX = newX;
    translateY = newY;
    updateTransform();
  }

  function onMouseUp(e) {
    if (!isDragging) return;
    isDragging = false;
    if (viewport) {
      viewport.style.cursor = "grab";
    }
    // If user clicked the backdrop without dragging, close
    if (!didDrag && (e.target === viewport || e.target === content)) {
      closeLightbox();
    }
  }

  function openLightbox(svgElement, root) {
    createLightbox();

    // Clone the SVG
    const clonedSvg = svgElement.cloneNode(true);

    // Read viewBox or fallback to boundingClientRect
    let vbW = 0;
    let vbH = 0;
    const viewBox = svgElement.getAttribute("viewBox") || clonedSvg.getAttribute("viewBox");
    if (viewBox) {
      const parts = viewBox.split(/[\s,]+/).map(parseFloat).filter((n) => !isNaN(n));
      if (parts.length >= 4 && parts[2] > 0 && parts[3] > 0) {
        vbW = parts[2];
        vbH = parts[3];
      }
    }

    if (!vbW || !vbH) {
      const rect = svgElement.getBoundingClientRect();
      if (rect.width > 0 && rect.height > 0) {
        vbW = rect.width;
        vbH = rect.height;
      }
    }

    // Safe fallbacks if dimensions are not detected
    if (!vbW || !vbH) {
      vbW = 850;
      vbH = 320;
    }

    // Compute scale to fit comfortably in viewport (~85vw x ~75vh)
    const maxW = Math.min(window.innerWidth * 0.85, 1200);
    const maxH = window.innerHeight * 0.75;
    const aspect = vbW / vbH;

    let displayW = maxW;
    let displayH = displayW / aspect;

    if (displayH > maxH) {
      displayH = maxH;
      displayW = displayH * aspect;
    }

    // Ensure a comfortable minimum size
    displayW = Math.max(Math.round(displayW), 450);
    displayH = Math.max(Math.round(displayH), Math.round(450 / aspect));

    // Set viewBox explicitly on cloned SVG and assign pixel dimensions
    clonedSvg.setAttribute("viewBox", viewBox || `0 0 ${vbW} ${vbH}`);
    clonedSvg.setAttribute("width", `${displayW}`);
    clonedSvg.setAttribute("height", `${displayH}`);

    clonedSvg.style.width = `${displayW}px`;
    clonedSvg.style.height = `${displayH}px`;
    clonedSvg.style.minWidth = `${displayW}px`;
    clonedSvg.style.minHeight = `${displayH}px`;
    clonedSvg.style.maxWidth = "none";
    clonedSvg.style.maxHeight = "none";
    clonedSvg.style.display = "block";
    clonedSvg.style.overflow = "visible";

    content.innerHTML = "";

    // Copy any styles from the shadow root so diagram styles are preserved
    if (root) {
      const styles = root.querySelectorAll("style");
      styles.forEach((s) => {
        content.appendChild(s.cloneNode(true));
      });
    }

    content.appendChild(clonedSvg);

    resetTransform();
    lightbox.classList.add("is-open");
    document.body.style.overflow = "hidden";
  }

  function closeLightbox() {
    if (!lightbox) return;
    lightbox.classList.remove("is-open");
    document.body.style.overflow = "";
    if (content) {
      content.innerHTML = "";
    }
  }

  // Event delegation on window using capture phase
  window.addEventListener(
    "click",
    function (e) {
      // Don't intercept clicks inside the open lightbox
      if (e.target.closest && e.target.closest(".mermaid-lightbox")) return;

      // Use composedPath to penetrate shadow DOM boundaries
      const path = e.composedPath ? e.composedPath() : [e.target];

      // Find the .mermaid container in the path
      let mermaidContainer = null;
      for (const el of path) {
        if (el.classList && el.classList.contains("mermaid")) {
          mermaidContainer = el;
          break;
        }
      }

      if (!mermaidContainer && e.target.closest) {
        mermaidContainer = e.target.closest(".mermaid");
      }

      if (!mermaidContainer) return;

      // Find the SVG: first check if an SVG is directly in the composed path
      let svg = null;
      for (const el of path) {
        if (el.tagName && el.tagName.toLowerCase() === "svg") {
          svg = el;
          break;
        }
      }

      // If not in path, check the shadow root or the container itself
      const root =
        mermaidContainer.shadowRoot ||
        mermaidContainer._mermaidShadowRoot ||
        shadowRoots.get(mermaidContainer);

      if (!svg && root) {
        svg = root.querySelector("svg");
      }
      if (!svg) {
        svg = mermaidContainer.querySelector("svg");
      }

      if (svg) {
        e.preventDefault();
        e.stopPropagation();
        openLightbox(svg, root);
      }
    },
    true // Capture phase to intercept before internal stopPropagation
  );
})();
