"use client";

import { UploadCloud } from "lucide-react";
import { useRef, useState, type DragEvent, type ReactNode } from "react";
import { cn } from "@/lib/utils";

export interface FileDropzoneProps {
  onFiles: (files: File[]) => void;
  accept?: string;
  multiple?: boolean;
  disabled?: boolean;
  title?: ReactNode;
  hint?: ReactNode;
  className?: string;
  compact?: boolean;
  children?: ReactNode;
}

/** Drag & drop (or click / keyboard) file picker. */
export function FileDropzone({
  onFiles,
  accept,
  multiple = true,
  disabled,
  title = "Drop files here or click to browse",
  hint,
  className,
  compact,
  children,
}: FileDropzoneProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);
  const depth = useRef(0);

  const accepts = (file: File) => {
    if (!accept) return true;
    const list = accept.split(",").map((a) => a.trim().toLowerCase());
    const name = file.name.toLowerCase();
    return list.some((a) =>
      a.startsWith(".") ? name.endsWith(a) : a.endsWith("/*") ? file.type.startsWith(a.slice(0, -1)) : file.type === a,
    );
  };

  const emit = (list: FileList | null) => {
    if (!list || disabled) return;
    const files = Array.from(list).filter(accepts);
    if (files.length) onFiles(multiple ? files : files.slice(0, 1));
  };

  const onDragEnter = (e: DragEvent) => {
    e.preventDefault();
    if (disabled) return;
    depth.current += 1;
    setDragging(true);
  };
  const onDragLeave = (e: DragEvent) => {
    e.preventDefault();
    depth.current = Math.max(0, depth.current - 1);
    if (depth.current === 0) setDragging(false);
  };
  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    depth.current = 0;
    setDragging(false);
    emit(e.dataTransfer.files);
  };

  return (
    <div
      role="button"
      tabIndex={disabled ? -1 : 0}
      aria-disabled={disabled || undefined}
      aria-label={typeof title === "string" ? title : "Upload files"}
      onClick={() => !disabled && inputRef.current?.click()}
      onKeyDown={(e) => {
        if ((e.key === "Enter" || e.key === " ") && !disabled) {
          e.preventDefault();
          inputRef.current?.click();
        }
      }}
      onDragEnter={onDragEnter}
      onDragOver={(e) => e.preventDefault()}
      onDragLeave={onDragLeave}
      onDrop={onDrop}
      className={cn(
        "group flex flex-col items-center justify-center rounded-card border border-dashed text-center transition-colors duration-150",
        compact ? "gap-1.5 px-4 py-5" : "gap-2.5 px-6 py-9",
        dragging ? "border-accent bg-[color-mix(in_srgb,var(--accent)_8%,transparent)]" : "border-border-strong bg-surface hover:bg-surface-2/60",
        disabled && "cursor-not-allowed opacity-50",
        className,
      )}
    >
      <input
        ref={inputRef}
        type="file"
        className="hidden"
        accept={accept}
        multiple={multiple}
        onChange={(e) => {
          emit(e.target.files);
          e.target.value = "";
        }}
      />
      <span
        className={cn(
          "flex items-center justify-center rounded-xl border border-border bg-surface-2 text-muted transition-transform group-hover:-translate-y-0.5",
          compact ? "size-8" : "size-10",
          dragging && "text-accent",
        )}
      >
        <UploadCloud className={compact ? "size-4" : "size-5"} />
      </span>
      <div>
        <p className="text-[13px] font-medium text-fg">{dragging ? "Release to upload" : title}</p>
        {hint && <p className="mt-0.5 text-xs text-muted">{hint}</p>}
      </div>
      {children}
    </div>
  );
}
