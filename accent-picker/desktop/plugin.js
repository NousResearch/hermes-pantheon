// src/plugin.tsx
import { $accentOverride as $accentOverride2, PALETTE_AREA, setAccentOverride as setAccentOverride2, STATUSBAR_AREAS } from "@hermes/plugin-sdk";

// src/picker.tsx
import {
  $accentOverride,
  contrastRatio,
  hexToOklch,
  maxChroma,
  oklchToHex,
  oklchToSrgb255,
  Popover,
  PopoverContent,
  PopoverTrigger,
  setAccentOverride,
  useTheme,
  useValue
} from "@hermes/plugin-sdk";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { jsx, jsxs } from "react/jsx-runtime";
var SWATCHES = [
  { hex: "#0053FD", name: "Nous blue \xB7 263\xB0 (light seed)" },
  { hex: "#4a84fe", name: "Nous blue \xB7 263\xB0 (dark seed)" },
  { hex: "#1540B1", name: "Psyche blue \xB7 264\xB0" },
  { hex: "#0969da", name: "GitHub blue \xB7 257\xB0" },
  { hex: "#196d31", name: "GitHub green \xB7 148\xB0" },
  { hex: "#8250df", name: "GitHub purple \xB7 303\xB0" },
  { hex: "#bf3989", name: "GitHub pink \xB7 354\xB0" },
  { hex: "#bc4c00", name: "GitHub orange \xB7 52\xB0" }
];
var FIELD_W = 224;
var FIELD_H = 130;
var RAIL_H = 12;
var HEX_CH = 7;
var CHROMA_MAX = 0.33;
var clamp01 = (n) => Math.min(1, Math.max(0, n));
function useDrag(onMove) {
  const ref = useRef(null);
  const moveRef = useRef(onMove);
  moveRef.current = onMove;
  const onPointerDown = useCallback((event) => {
    const box = ref.current;
    if (!box) {
      return;
    }
    const emit = (clientX, clientY) => {
      const r = box.getBoundingClientRect();
      moveRef.current(clamp01((clientX - r.left) / r.width), clamp01((clientY - r.top) / r.height));
    };
    emit(event.clientX, event.clientY);
    const onPointerMove = (e) => emit(e.clientX, e.clientY);
    const stop = () => {
      window.removeEventListener("pointermove", onPointerMove);
      window.removeEventListener("pointerup", stop);
    };
    window.addEventListener("pointermove", onPointerMove);
    window.addEventListener("pointerup", stop);
  }, []);
  return { onPointerDown, ref };
}
function ChromaLightnessField({
  hue,
  lch,
  onPick
}) {
  const canvas = useRef(null);
  const drag = useDrag((x, y) => onPick(1 - y, x * CHROMA_MAX));
  useEffect(() => {
    const el = canvas.current;
    if (!el) {
      return;
    }
    const ctx = el.getContext("2d");
    if (!ctx) {
      return;
    }
    const img = ctx.createImageData(FIELD_W, FIELD_H);
    for (let y = 0; y < FIELD_H; y += 1) {
      const l = 1 - y / (FIELD_H - 1);
      for (let x = 0; x < FIELD_W; x += 1) {
        const rgb = oklchToSrgb255({ l, c: x / (FIELD_W - 1) * CHROMA_MAX, h: hue });
        const i = (y * FIELD_W + x) * 4;
        if (rgb) {
          img.data[i] = rgb[0];
          img.data[i + 1] = rgb[1];
          img.data[i + 2] = rgb[2];
          img.data[i + 3] = 255;
        } else {
          img.data[i + 3] = 0;
        }
      }
    }
    ctx.clearRect(0, 0, FIELD_W, FIELD_H);
    ctx.putImageData(img, 0, 0);
  }, [hue]);
  return /* @__PURE__ */ jsxs(
    "div",
    {
      className: "relative cursor-crosshair overflow-hidden rounded-sm",
      onPointerDown: drag.onPointerDown,
      ref: drag.ref,
      style: { height: FIELD_H, width: FIELD_W },
      children: [
        /* @__PURE__ */ jsx("canvas", { className: "block size-full", height: FIELD_H, ref: canvas, width: FIELD_W }),
        /* @__PURE__ */ jsx(
          "div",
          {
            className: "pointer-events-none absolute size-3 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-white shadow-[0_0_0_1px_rgba(0,0,0,.6)]",
            style: { left: `${lch.c / CHROMA_MAX * 100}%`, top: `${(1 - lch.l) * 100}%` }
          }
        )
      ]
    }
  );
}
function HueRail({ lch, onPick }) {
  const drag = useDrag((x) => onPick(x * 360));
  const gradient = useMemo(() => {
    const stops = [];
    for (let i = 0; i <= 36; i += 1) {
      const h = i / 36 * 360;
      const c = Math.min(lch.c, maxChroma(lch.l, h));
      stops.push(`${oklchToHex({ l: lch.l, c, h })} ${i / 36 * 100}%`);
    }
    return `linear-gradient(to right, ${stops.join(", ")})`;
  }, [lch.l, lch.c]);
  return /* @__PURE__ */ jsx(
    "div",
    {
      className: "relative cursor-ew-resize rounded-full",
      onPointerDown: drag.onPointerDown,
      ref: drag.ref,
      style: { background: gradient, height: RAIL_H, width: FIELD_W },
      children: /* @__PURE__ */ jsx(
        "div",
        {
          className: "pointer-events-none absolute top-1/2 size-3.5 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-white shadow-[0_0_0_1px_rgba(0,0,0,.6)]",
          style: { left: `${lch.h / 360 * 100}%` }
        }
      )
    }
  );
}
function AccentPicker() {
  const { theme, renderedMode } = useTheme();
  const override = useValue($accentOverride);
  const painted = theme.colors.primary;
  const [lch, setLch] = useState(() => hexToOklch(painted) ?? { l: 0.5, c: 0.15, h: 260 });
  const [text, setText] = useState(painted);
  const dragging = useRef(false);
  useEffect(() => {
    if (!dragging.current) {
      setLch(hexToOklch(painted) ?? { l: 0.5, c: 0.15, h: 260 });
      setText(painted);
    }
  }, [painted]);
  const commit = (next) => {
    dragging.current = true;
    setLch(next);
    const hex = oklchToHex(next);
    setText(hex);
    setAccentOverride(hex);
    requestAnimationFrame(() => {
      dragging.current = false;
    });
  };
  const surface = (renderedMode === "dark" ? theme.darkColors : theme.colors) ?? theme.colors;
  const ratio = contrastRatio(painted, surface.sidebarBackground ?? surface.background);
  return /* @__PURE__ */ jsxs("div", { className: "flex flex-col gap-2 p-2", style: { width: FIELD_W + 16 }, children: [
    /* @__PURE__ */ jsx(ChromaLightnessField, { hue: lch.h, lch, onPick: (l, c) => commit({ ...lch, c, l }) }),
    /* @__PURE__ */ jsx(HueRail, { lch, onPick: (h) => commit({ ...lch, h }) }),
    /* @__PURE__ */ jsxs("div", { className: "flex items-center gap-1.5", children: [
      /* @__PURE__ */ jsx("span", { className: "size-5 shrink-0 rounded-sm border border-(--dt-border)", style: { background: painted } }),
      /* @__PURE__ */ jsx(
        "input",
        {
          className: "min-w-0 flex-1 rounded-sm border border-(--dt-border) bg-transparent px-1.5 py-0.5 font-mono text-[11px] uppercase",
          onChange: (event) => {
            setText(event.target.value);
            setAccentOverride(event.target.value);
          },
          spellCheck: false,
          value: text
        }
      ),
      /* @__PURE__ */ jsx(
        "button",
        {
          className: "shrink-0 rounded-sm border border-(--dt-border) px-1.5 py-0.5 text-[11px]",
          onClick: () => setAccentOverride(null),
          type: "button",
          children: "reset"
        }
      )
    ] }),
    /* @__PURE__ */ jsx("div", { style: { display: "grid", gridTemplateColumns: "repeat(8, minmax(0, 1fr))", gap: "4px" }, children: SWATCHES.map((swatch) => /* @__PURE__ */ jsx(
      "button",
      {
        className: "size-5 rounded-sm border border-(--dt-border)",
        onClick: () => setAccentOverride(swatch.hex),
        style: { background: swatch.hex },
        title: `${swatch.name} \xB7 ${swatch.hex}`,
        type: "button"
      },
      swatch.hex
    )) }),
    /* @__PURE__ */ jsxs("div", { className: "flex items-center justify-between font-mono text-[10px] tabular-nums text-(--ui-text-tertiary)", children: [
      /* @__PURE__ */ jsxs("span", { children: [
        "H",
        /* @__PURE__ */ jsx("span", { className: "inline-block w-[3ch] text-right", children: Math.round(lch.h) }),
        " L",
        lch.l.toFixed(2),
        " C",
        lch.c.toFixed(3)
      ] }),
      /* @__PURE__ */ jsxs("span", { className: ratio >= 4.5 ? "" : "text-(--dt-destructive)", children: [
        renderedMode,
        " ",
        ratio.toFixed(1),
        ":1"
      ] })
    ] }),
    /* @__PURE__ */ jsx("p", { className: "h-3 font-mono text-[10px] leading-tight text-(--ui-text-tertiary)", children: override && override.toLowerCase() !== painted.toLowerCase() ? `picked ${override} \u2192 ${painted} for contrast` : "\xA0" })
  ] });
}
function AccentPickerTrigger() {
  const { theme } = useTheme();
  return /* @__PURE__ */ jsxs(Popover, { children: [
    /* @__PURE__ */ jsx(PopoverTrigger, { asChild: true, children: /* @__PURE__ */ jsxs(
      "button",
      {
        className: "inline-flex h-full items-center gap-1.5 px-1.5 text-[0.6875rem] text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover) hover:text-foreground",
        title: "Accent color (dev)",
        type: "button",
        children: [
          /* @__PURE__ */ jsx(
            "span",
            {
              className: "size-2.5 shrink-0 rounded-full border border-(--dt-border)",
              style: { background: theme.colors.primary }
            }
          ),
          /* @__PURE__ */ jsx("span", { className: "text-center font-mono tabular-nums", style: { width: `${HEX_CH}ch` }, children: theme.colors.primary })
        ]
      }
    ) }),
    /* @__PURE__ */ jsx(PopoverContent, { align: "end", className: "w-auto p-0", side: "top", children: /* @__PURE__ */ jsx(AccentPicker, {}) })
  ] });
}

// src/plugin.tsx
import { jsx as jsx2 } from "react/jsx-runtime";
var plugin = {
  id: "accent",
  name: "Accent Picker",
  description: "Pick the theme accent from an OKLCH color picker in the status bar; the palette re-derives live. Authoring tool \u2014 the color is not persisted.",
  defaultEnabled: false,
  register(ctx) {
    ctx.onDispose(() => setAccentOverride2(null));
    ctx.registerMany([
      {
        id: "picker",
        area: STATUSBAR_AREAS.right,
        order: 90,
        render: () => /* @__PURE__ */ jsx2(AccentPickerTrigger, {})
      },
      {
        id: "reset",
        area: PALETTE_AREA,
        data: {
          id: "accent.reset",
          label: "Accent: reset to the theme default",
          keywords: ["accent", "color", "theme", "reset", "default"],
          run: () => setAccentOverride2(null)
        }
      },
      {
        id: "copy",
        area: PALETTE_AREA,
        data: {
          id: "accent.copy",
          label: "Accent: copy the current color",
          keywords: ["accent", "color", "hex", "copy", "clipboard"],
          run: () => {
            const hex = $accentOverride2.get();
            if (hex) {
              void navigator.clipboard?.writeText(hex);
            }
          }
        }
      }
    ]);
  }
};
var plugin_default = plugin;
export {
  plugin_default as default
};
