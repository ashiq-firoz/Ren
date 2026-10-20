"use client";

import { ArrowRight, Briefcase, Check, User } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";
import { Button } from "@/components/ui/button";
import { Field, Input } from "@/components/ui/input";
import { getList } from "@/hooks/queries";
import { errorMessage } from "@/lib/api";
import type { Provider, WorkspaceMode } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useAuth } from "@/providers/auth";
import { AuthHeading, FormError, PasswordInput } from "../auth-form";

function ModeCard({
  active,
  onClick,
  icon,
  title,
  description,
}: {
  active: boolean;
  onClick: () => void;
  icon: ReactNode;
  title: string;
  description: string;
}) {
  return (
    <button
      type="button"
      role="radio"
      aria-checked={active}
      onClick={onClick}
      className={cn(
        "relative flex flex-col items-start gap-1.5 rounded-card border p-3 text-left transition-colors",
        active ? "border-fg bg-surface" : "border-border-strong bg-transparent hover:bg-surface",
      )}
    >
      <span className="flex w-full items-center justify-between">
        <span className="flex size-7 items-center justify-center rounded-md bg-surface-2 text-fg [&_svg]:size-4">{icon}</span>
        {active && (
          <span className="flex size-4 items-center justify-center rounded-full bg-primary text-primary-fg">
            <Check className="size-2.5" strokeWidth={3} />
          </span>
        )}
      </span>
      <span className="text-[13px] font-semibold text-fg">{title}</span>
      <span className="text-xs leading-snug text-muted">{description}</span>
    </button>
  );
}

export default function RegisterPage() {
  const { register, status } = useAuth();
  const router = useRouter();
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [orgName, setOrgName] = useState("");
  const [mode, setMode] = useState<WorkspaceMode>("business");
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  useEffect(() => {
    if (status === "authenticated" && !pending) router.replace("/chat");
  }, [status, pending, router]);

  const tooShort = password.length > 0 && password.length < 8;

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (tooShort) return;
    setError(null);
    setPending(true);
    try {
      await register({
        name: name.trim(),
        email: email.trim(),
        password,
        org_name: orgName.trim() || undefined,
        mode,
      });
      // First run: connect a model provider if the workspace has none.
      let target = "/chat";
      try {
        const providers = await getList<Provider>("/providers");
        if (providers.items.length === 0) target = "/onboarding";
      } catch {
        target = "/onboarding";
      }
      router.replace(target);
    } catch (err) {
      setError(errorMessage(err, "Couldn't create your account"));
      setPending(false);
    }
  };

  const defaultOrg = name.trim() ? `${name.trim().split(/\s+/)[0]}'s Workspace` : "Acme Operations";

  return (
    <>
      <AuthHeading title="Start your swarm" subtitle="Create an account and a workspace. Takes a minute." />
      <form onSubmit={submit} className="space-y-4" noValidate>
        <FormError message={error} />
        <div role="radiogroup" aria-label="Workspace mode" className="grid grid-cols-2 gap-2.5">
          <ModeCard
            active={mode === "business"}
            onClick={() => setMode("business")}
            icon={<Briefcase />}
            title="Business"
            description="A team of agents for your organization"
          />
          <ModeCard
            active={mode === "personal"}
            onClick={() => setMode("personal")}
            icon={<User />}
            title="Personal"
            description="Your private agent swarm"
          />
        </div>
        <Field label="Your name">
          <Input autoComplete="name" required value={name} onChange={(e) => setName(e.target.value)} placeholder="Ada Lovelace" />
        </Field>
        <Field label="Work email">
          <Input
            type="email"
            autoComplete="email"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="you@company.com"
          />
        </Field>
        <Field label="Password" error={tooShort ? "Use at least 8 characters." : undefined}>
          <PasswordInput
            autoComplete="new-password"
            required
            value={password}
            invalid={tooShort}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="At least 8 characters"
          />
        </Field>
        <Field label={mode === "business" ? "Workspace name" : "Workspace name (optional)"} hint="You can rename it later.">
          <Input value={orgName} onChange={(e) => setOrgName(e.target.value)} placeholder={defaultOrg} />
        </Field>
        <Button
          type="submit"
          variant="primary"
          size="lg"
          className="w-full"
          loading={pending}
          disabled={!name.trim() || !email.trim() || password.length < 8}
          iconRight={!pending ? <ArrowRight className="size-4" /> : undefined}
        >
          Create account
        </Button>
      </form>
      <p className="mt-6 text-center text-[13px] text-muted">
        Already have an account?{" "}
        <Link href="/login" className="font-medium text-fg underline underline-offset-4 hover:text-accent">
          Sign in
        </Link>
      </p>
    </>
  );
}
