import { useState } from "react";
import type { PapersApi, ProjectInfo } from "../../api-client/papers";
import { Modal } from "./Modal";

interface Props {
  papers: PapersApi;
  projects: Array<ProjectInfo>;
  onClose: () => void;
  /** 上传被后端接受后回调新论文 ID。 */
  onUploaded: (paperId: string) => void;
}

const NEW_PROJECT = "__new__";
const MAX_BYTES = 100 * 1024 * 1024;

/** 把本地 PDF 上传到某个项目，上传后出现在书架上（与新建实验共用同一份论文）。 */
export function UploadPdfDialog({ papers, projects, onClose, onUploaded }: Props) {
  const [projectId, setProjectId] = useState(
    projects.length ? projects[projects.length - 1].id : NEW_PROJECT,
  );
  const [projectName, setProjectName] = useState("论文阅读");
  const [file, setFile] = useState<File | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  // 一次提交固定一个幂等键，重复点击不会重复上传。
  const [uploadKey] = useState(() => crypto.randomUUID());
  const [projectKey] = useState(() => crypto.randomUUID());

  const submit = async () => {
    if (!file) {
      setError("请选择一个 PDF 文件");
      return;
    }
    if (file.type && file.type !== "application/pdf") {
      setError("只能上传 PDF 文件");
      return;
    }
    if (file.size > MAX_BYTES) {
      setError("文件超过 100 MB");
      return;
    }
    if (projectId === NEW_PROJECT && !projectName.trim()) {
      setError("请填写新项目名称");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const target =
        projectId === NEW_PROJECT
          ? (await papers.createProject(projectName.trim(), projectKey)).id
          : projectId;
      const accepted = await papers.uploadPaperPdf(target, file, {
        titleHint: file.name.replace(/\.pdf$/i, ""),
        key: uploadKey,
      });
      onUploaded(accepted.paper_id);
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      title="上传 PDF"
      onClose={busy ? () => {} : onClose}
      footer={
        <>
          <button
            type="button"
            className="btn btn-secondary"
            disabled={busy}
            onClick={onClose}
          >
            取消
          </button>
          <button
            type="button"
            className="btn btn-primary"
            disabled={busy}
            onClick={() => void submit()}
          >
            {busy ? "上传中…" : "上传并打开"}
          </button>
        </>
      }
    >
      <label className="field-label" htmlFor="upload-pdf-file">
        PDF 文件
      </label>
      <input
        id="upload-pdf-file"
        className="field-input"
        type="file"
        accept="application/pdf,.pdf"
        disabled={busy}
        onChange={(event) => {
          setFile(event.target.files?.[0] ?? null);
          setError("");
        }}
      />
      <label className="field-label" htmlFor="upload-pdf-project">
        所属项目
      </label>
      <select
        id="upload-pdf-project"
        className="field-input"
        value={projectId}
        disabled={busy}
        onChange={(event) => setProjectId(event.target.value)}
      >
        {projects.map((p) => (
          <option key={p.id} value={p.id}>
            {p.name}
          </option>
        ))}
        <option value={NEW_PROJECT}>新建项目…</option>
      </select>
      {projectId === NEW_PROJECT ? (
        <>
          <label className="field-label" htmlFor="upload-pdf-project-name">
            新项目名称
          </label>
          <input
            id="upload-pdf-project-name"
            className="field-input"
            value={projectName}
            disabled={busy}
            onChange={(event) => setProjectName(event.target.value)}
          />
        </>
      ) : null}
      <p className="field-error" role={error ? "alert" : undefined}>
        {error}
      </p>
    </Modal>
  );
}
