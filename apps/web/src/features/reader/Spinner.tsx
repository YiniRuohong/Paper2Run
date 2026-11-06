import "./spinner.css";

export function Spinner() {
  return (
    <div className="pdf-loading">
      <div className="pdf-loading-ring" aria-hidden="true" />
      <p className="pdf-loading-text">正在加载 PDF…</p>
    </div>
  );
}
