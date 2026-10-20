"use client";

import { ArrowRight } from "lucide-react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Field, Input } from "@/components/ui/input";
import { errorMessage } from "@/lib/api";
import { useAuth } from "@/providers/auth";
import { AuthHeading, FormError, PasswordInput, safeNext } from "../auth-form";

function LoginForm() {
  const { login, status } = useAuth();
  const router = useRouter();
  const params = useSearchParams();
  const next = safeNext(params.get("next"));
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  useEffect(() => {
    if (status === "authenticated") router.replace(next);
  }, [status, next, router]);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setPending(true);
    try {
      await login(email.trim(), password);
      router.replace(next);
    } catch (err) {
      setError(errorMessage(err, "Couldn't sign you in"));
      setPending(false);
    }
  };

  return (
    <form onSubmit={submit} className="space-y-4" noValidate>
      <FormError message={error} />
      <Field label="Email">
        <Input
          type="email"
          autoComplete="email"
          autoFocus
          required
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          placeholder="you@company.com"
        />
      </Field>
      <Field label="Password">
        <PasswordInput
          autoComplete="current-password"
          required
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          placeholder="••••••••"
        />
      </Field>
      <Button
        type="submit"
        variant="primary"
        size="lg"
        className="w-full"
        loading={pending}
        disabled={!email.trim() || !password}
        iconRight={!pending ? <ArrowRight className="size-4" /> : undefined}
      >
        Sign in
      </Button>
    </form>
  );
}

export default function LoginPage() {
  return (
    <>
      <AuthHeading title="Welcome back" subtitle="Sign in to your swarm." />
      <Suspense fallback={<div className="h-[220px]" />}>
        <LoginForm />
      </Suspense>
      <p className="mt-6 text-center text-[13px] text-muted">
        New to Ren?{" "}
        <Link href="/register" className="font-medium text-fg underline underline-offset-4 hover:text-accent">
          Create an account
        </Link>
      </p>
    </>
  );
}
