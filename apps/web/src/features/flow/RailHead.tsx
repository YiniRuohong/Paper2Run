// Top of the timeline rail on the full-bleed flow pages (draft and task):
// the brand and the way back to the overview. Nothing else lives here.
export default function RailHead() {
  return <>
    <b className="flow-brand">Paper2Run</b>
    <a className="flow-back" href="#/">← 总览</a>
  </>;
}
