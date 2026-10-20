"use client";

import { useQuery } from "@tanstack/react-query";
import { ArrowRight, Building2, MailOpen } from "lucide-react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useState } from "react";
import { RoleBadge } from "@/components/domain/status";
import { Button } from "@/components/ui/button";
import { ErrorState, Skeleton } from "@/components/ui/feedback";
import { Field, Input } from "@/components/ui/input";
import { api, errorMessage } from "@/lib/api";
import type { InviteInfo } from "@/lib/types";
import { useAuth } from "@/providers/auth";
import { AuthHeading, FormError, PasswordInput } from "../../auth-form";

export default function InvitePage() {
  const { token } = useParams<{ token: string }>();
  const router = useRouter();
  const { acceptInvite } = useAuth();
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  const invite = useQuery({
    queryKey: ["invite", token],
    queryFn: () => api.get<InviteInfo>(`/auth/invite/${token}`),
    enabled: !!token,
    retry: false,
  });

  if (invite.isLoading) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-10 w-3/4" />
        <Skeleton className="h-4 w-1/2" />
        <Skeleton className="mt-6 h-24 w-full rounded-card" />
      </div>
    );
  }

  if (invite.error || !invite.data) {
    return (
      <div className="text-center">
        <ErrorState
          error={invite.error ?? new Error("Invite not found")}
          title="This invite isn't valid"
          onRetry={() => invite.refetch()}
        />
        <Link href="/login" className="text-[13px] font-medium text-fg underline underline-offset-4">
          Go to sign in
        </Link>
      </div>
    );
  }

  const info = invite.data;
  const existing = info.user_exists;

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setPending(true);
    try {
      await acceptInvite(existing ? { token, password } : { token, name: name.trim(), password }, info.org_name);
      router.replace("/chat");
    } catch (err) {
      setError(errorMessage(err, "Couldn't accept the invite"));
      setPending(false);
    }
  };

  return (
    <>
      <AuthHeading
        title={`Join ${info.org_name}`}
        subtitle={`${info.inviter_name || "A teammate"} invited you to collaborate with their agent swarm.`}
      />
      <div className="mb-6 flex items-center gap-3 rounded-card border border-border bg-surface p-3.5">
        <span className="flex size-10 items-center justify-center rounded-lg bg-surface-2">
          <Building2 className="size-5 text-muted" />
        </span>
        <div className="min-w-0 flex-1">
          <p className="truncate text-[13.5px] font-semibold text-fg">{info.org_name}</p>
          <p className="flex items-center gap-1.5 truncate text-xs text-muted">
            <MailOpen className="size-3" /> {info.email}
          </p>
        </div>
        <RoleBadge role={info.role} />
      </div>
      <form onSubmit={submit} className="space-y-4">
        <FormError message={error} />
        {existing ? (
          <p className="text-[13px] text-muted">You already have a Ren account. Enter your password to accept.</p>
        ) : (
          <Field label="Your name">
            <Input autoComplete="name" required value={name} onChange={(e) => setName(e.target.value)} placeholder="Ada Lovelace" />
          </Field>
        )}
        <Field label={existing ? "Password" : "Choose a password"}>
          <PasswordInput
            autoComplete={existing ? "current-password" : "new-password"}
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder={existing ? "Your password" : "At least 8 characters"}
          />
        </Field>
        <Button
          type="submit"
          variant="primary"
          size="lg"
          className="w-full"
          loading={pending}
          disabled={!password || (!existing && (!name.trim() || password.length < 8))}
          iconRight={!pending ? <ArrowRight className="size-4" /> : undefined}
        >
          {existing ? "Sign in & join" : "Create account & join"}
        </Button>
      </form>
    </>
  );
}
