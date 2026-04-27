// The FactoryControlPanel used to be a flat row of buttons. The pixel
// office now uses a game-style ControlDock at the bottom of the page,
// so this file forwards to that instead. Kept as a shim for legacy
// imports.
import ControlDock from "./ControlDock.jsx";

export default function FactoryControlPanel({ factory, onChanged }) {
  return <ControlDock factory={factory} runners={[]} onChanged={onChanged} />;
}
