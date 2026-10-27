"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useRef, useState, type FormEvent } from "react";

import { useAuth } from "@/components/auth-provider";
import { Alert, Button, Field, Input } from "@/components/ui";
import { api } from "@/lib/api/client";
import { ApiError, describeError, fieldErrors } from "@/lib/api/errors";
import { MAX_EMAIL_CHARS, MAX_PASSWORD_CHARS, MIN_PASSWORD_CHARS } from "@/lib/api/types";
import { safeNextPath } from "@/lib/format";

// Same rule as the backend (app/schemas/auth.py): one "@", a dotted domain, no spaces.
const EMAIL = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;

type Errors = Partial<Record<"email" | "password" | "confirm", string>>;

function validate(email: string, password: string, mode: "login" | "register", confirm = ""): Errors {
  const errors: Errors = {};
  if (!EMAIL.test(email.trim())) errors.email = "Enter a valid email address.";
  if (mode === "login" && !password) errors.password = "Enter your password.";
  if (mode === "register") {
    if (password.length < MIN_PASSWORD_CHARS) {
      errors.password = `Use at least ${MIN_PASSWORD_CHARS} characters.`;
    } else if (!password.trim()) {
      errors.password = "The password cannot be only spaces.";
    }
    if (confirm !== password) errors.confirm = "The passwords do not match.";
  }
  return errors;
}

function useRedirectWhenSignedIn() {
  const { status } = useAuth();
  const router = useRouter();
  const next = safeNextPath(useSearchParams().get("next"));
  useEffect(() => {
    if (status === "authenticated") router.replace(next);
  }, [status, next, router]);
  return next;
}

export function LoginForm() {
  const { signIn } = useAuth();
  useRedirectWhenSignedIn();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [errors, setErrors] = useState<Errors>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const submitting = useRef(false);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (submitting.current) return;
    const found = validate(email, password, "login");
    setErrors(found);
    setFormError(null);
    if (Object.keys(found).length) return;
    submitting.current = true;
    setPending(true);
    try {
      await signIn(email.trim(), password);
      // useRedirectWhenSignedIn navigates once the session is authenticated.
    } catch (error) {
      setPassword("");
      setFormError(
        error instanceof ApiError && error.code === "invalid_credentials"
          ? "Incorrect email or password."
          : describeError(error),
      );
    } finally {
      submitting.current = false;
      setPending(false);
    }
  }

  return (
    <form onSubmit={onSubmit} noValidate className="space-y-5">
      <div className="space-y-1">
        <h1 className="text-2xl font-semibold tracking-tight">Sign in</h1>
        <p className="text-sm text-muted">Welcome back. Sign in to search and ask your documents.</p>
      </div>
      {formError && <Alert>{formError}</Alert>}
      <Field label="Email" error={errors.email}>
        {({ id, describedBy, invalid }) => (
          <Input
            id={id}
            type="email"
            autoComplete="email"
            inputMode="email"
            maxLength={MAX_EMAIL_CHARS}
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            aria-invalid={invalid}
            aria-describedby={describedBy}
            required
          />
        )}
      </Field>
      <Field label="Password" error={errors.password}>
        {({ id, describedBy, invalid }) => (
          <Input
            id={id}
            type="password"
            autoComplete="current-password"
            maxLength={MAX_PASSWORD_CHARS}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            aria-invalid={invalid}
            aria-describedby={describedBy}
            required
          />
        )}
      </Field>
      <Button type="submit" pending={pending} className="w-full">
        {pending ? "Signing in" : "Sign in"}
      </Button>
      <p className="text-center text-sm text-muted">
        New to DocMind?{" "}
        <Link href="/register" className="font-medium text-accent hover:underline">
          Create an account
        </Link>
      </p>
    </form>
  );
}

export function RegisterForm() {
  const { signIn } = useAuth();
  const router = useRouter();
  useRedirectWhenSignedIn();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [errors, setErrors] = useState<Errors>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const submitting = useRef(false);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (submitting.current) return;
    const found = validate(email, password, "register", confirm);
    setErrors(found);
    setFormError(null);
    if (Object.keys(found).length) return;
    submitting.current = true;
    setPending(true);
    const credentials = { email: email.trim(), password };
    try {
      await api.register(credentials);
    } catch (error) {
      if (error instanceof ApiError && error.code === "email_already_registered") {
        setFormError("An account with this email already exists. Sign in instead.");
      } else {
        const fields = fieldErrors(error);
        setErrors({ email: fields.email, password: fields.password });
        setFormError(Object.keys(fields).length ? null : describeError(error));
      }
      submitting.current = false;
      setPending(false);
      return;
    }
    try {
      await signIn(credentials.email, credentials.password);
    } catch {
      // The account exists; only the automatic sign-in failed (e.g. rate limited).
      router.replace("/login");
    } finally {
      submitting.current = false;
      setPending(false);
    }
  }

  return (
    <form onSubmit={onSubmit} noValidate className="space-y-5">
      <div className="space-y-1">
        <h1 className="text-2xl font-semibold tracking-tight">Create your account</h1>
        <p className="text-sm text-muted">Your documents stay private to this account.</p>
      </div>
      {formError && <Alert>{formError}</Alert>}
      <Field label="Email" error={errors.email}>
        {({ id, describedBy, invalid }) => (
          <Input
            id={id}
            type="email"
            autoComplete="email"
            inputMode="email"
            maxLength={MAX_EMAIL_CHARS}
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            aria-invalid={invalid}
            aria-describedby={describedBy}
            required
          />
        )}
      </Field>
      <Field
        label="Password"
        hint={`At least ${MIN_PASSWORD_CHARS} characters. A long passphrase works well.`}
        error={errors.password}
      >
        {({ id, describedBy, invalid }) => (
          <Input
            id={id}
            type="password"
            autoComplete="new-password"
            minLength={MIN_PASSWORD_CHARS}
            maxLength={MAX_PASSWORD_CHARS}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            aria-invalid={invalid}
            aria-describedby={describedBy}
            required
          />
        )}
      </Field>
      <Field label="Confirm password" error={errors.confirm}>
        {({ id, describedBy, invalid }) => (
          <Input
            id={id}
            type="password"
            autoComplete="new-password"
            maxLength={MAX_PASSWORD_CHARS}
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            aria-invalid={invalid}
            aria-describedby={describedBy}
            required
          />
        )}
      </Field>
      <Button type="submit" pending={pending} className="w-full">
        {pending ? "Creating account" : "Create account"}
      </Button>
      <p className="text-center text-sm text-muted">
        Already have an account?{" "}
        <Link href="/login" className="font-medium text-accent hover:underline">
          Sign in
        </Link>
      </p>
    </form>
  );
}
