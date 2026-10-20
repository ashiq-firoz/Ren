import Link from "next/link";
import { Wordmark } from "@/components/brand/logo";

export default function NotFound() {
  return (
    <div className="flex min-h-dvh flex-col items-center justify-center gap-6 bg-bg px-6 text-center">
      <Wordmark />
      <div>
        <p className="text-xs font-medium tracking-[0.12em] text-subtle uppercase">404</p>
        <h1 className="font-serif-display mt-2 text-[40px] leading-tight text-fg">Nothing linked here</h1>
        <p className="mt-2 text-[13.5px] text-muted">The page you&apos;re looking for doesn&apos;t exist or has moved.</p>
      </div>
      <Link
        href="/chat"
        className="inline-flex h-9 items-center rounded-control bg-primary px-4 text-sm font-medium text-primary-fg hover:opacity-90"
      >
        Back to Ren
      </Link>
    </div>
  );
}
