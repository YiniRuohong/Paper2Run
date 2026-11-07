import type { JSX } from "react";
import { GlobalWorkerOptions, getDocument } from "pdfjs-dist";
import type { PDFDocumentProxy } from "pdfjs-dist";
import React, { Component } from "react";
// 本地打包 worker：应用只在回环地址运行，不能从 CDN 加载。
import localWorkerSrc from "pdfjs-dist/build/pdf.worker.min.mjs?url";

interface Props {
  /** See `GlobalWorkerOptionsType`. */
  workerSrc: string;

  url: string;
  beforeLoad: JSX.Element;
  errorMessage?: JSX.Element;
  children: (pdfDocument: PDFDocumentProxy) => JSX.Element;
  onError?: (error: Error) => void;
  cMapUrl?: string;
  cMapPacked?: boolean;
  /** Extra request headers for the PDF fetch (pdf.js `httpHeaders`). */
  httpHeaders?: Record<string, string>;
}

interface State {
  pdfDocument: PDFDocumentProxy | null;
  error: Error | null;
}

export class PdfLoader extends Component<Props, State> {
  state: State = {
    pdfDocument: null,
    error: null,
  };

  static defaultProps = {
    workerSrc: localWorkerSrc,
  };

  documentRef = React.createRef<HTMLElement>();

  componentDidMount() {
    this.load();
  }

  componentWillUnmount() {
    const { pdfDocument: discardedDocument } = this.state;
    if (discardedDocument) {
      discardedDocument.destroy();
    }
  }

  componentDidUpdate({ url }: Props) {
    if (this.props.url !== url) {
      this.load();
    }
  }

  componentDidCatch(error: Error) {
    const { onError } = this.props;

    if (onError) {
      onError(error);
    }

    this.setState({ pdfDocument: null, error });
  }

  load() {
    const { ownerDocument = document } = this.documentRef.current || {};
    const { url, cMapUrl, cMapPacked, workerSrc } = this.props;
    const { pdfDocument: discardedDocument } = this.state;
    this.setState({ pdfDocument: null, error: null });

    if (typeof workerSrc === "string") {
      GlobalWorkerOptions.workerSrc = workerSrc;
    }

    Promise.resolve()
      .then(() => discardedDocument?.destroy())
      .then(() => {
        if (!url) {
          return;
        }

        const document = {
          ...this.props,
          ownerDocument,
          cMapUrl,
          cMapPacked,
        };

        return getDocument(document).promise.then((pdfDocument) => {
          this.setState({ pdfDocument });
        });
      })
      .catch((e) => this.componentDidCatch(e));
  }

  render() {
    const { children, beforeLoad } = this.props;
    const { pdfDocument, error } = this.state;
    return (
      <>
        <span ref={this.documentRef} />
        {error
          ? this.renderError()
          : !pdfDocument || !children
            ? beforeLoad
            : children(pdfDocument)}
      </>
    );
  }

  renderError() {
    const { errorMessage } = this.props;
    if (errorMessage) {
      return React.cloneElement(errorMessage, { error: this.state.error });
    }

    return null;
  }
}
