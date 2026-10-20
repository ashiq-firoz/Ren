"use client";

import { createContext, useCallback, useContext, type MouseEvent } from "react";
import type { FileSource } from "@/lib/files";

export interface FileViewerContextValue {
  /** Open a file in the side viewer. */
  openFile: (source: FileSource) => void;
  closeFile: () => void;
}

// Lives apart from the provider so FileLink (rendered inside Markdown, which the viewer also uses) has no import cycle.
export const FileViewerContext = createContext<FileViewerContextValue | null>(null);

/** The viewer of the authenticated app shell. */
export function useFileViewer(): FileViewerContextValue {
  const ctx = useContext(FileViewerContext);
  if (!ctx) throw new Error("useFileViewer must be used inside FileViewerProvider");
  return ctx;
}

/** Null outside the provider (callers fall back to navigating to the full-page viewer). */
export function useOptionalFileViewer(): FileViewerContextValue | null {
  return useContext(FileViewerContext);
}

/**
 * onClick for links to the full-page viewer: a plain click opens the side viewer instead;
 * modified / middle clicks (new tab) and use outside the provider keep normal navigation.
 */
export function useFileClick() {
  const viewer = useOptionalFileViewer();
  return useCallback(
    (source: FileSource) => (e: MouseEvent<HTMLElement>) => {
      if (!viewer || e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
      e.preventDefault();
      e.stopPropagation();
      viewer.openFile(source);
    },
    [viewer],
  );
}
