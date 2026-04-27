/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,jsx}"],
  theme: {
    extend: {
      colors: {
        floor: "#0f172a",
        floorTile: "#111c33",
        deskTop: "#1e293b",
        deskEdge: "#334155",
        wall: "#0b1221",
        accent: "#38bdf8",
        glow: "#7dd3fc",
      },
      boxShadow: {
        deskGlow: "0 0 24px rgba(125, 211, 252, 0.18)",
      },
      fontFamily: {
        mono: [
          "ui-monospace",
          "SFMono-Regular",
          "Menlo",
          "Monaco",
          "Consolas",
          "monospace",
        ],
      },
    },
  },
  plugins: [],
};
