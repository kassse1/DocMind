import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { LoginForm, RegisterForm } from "@/components/auth-forms";
import { AuthProvider } from "@/components/auth-provider";
import { FIXTURE_TOKEN, errorResponse, jsonResponse, mockFetch } from "./helpers";

const replace = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace, push: vi.fn() }),
  useSearchParams: () => new URLSearchParams("next=/documents"),
  usePathname: () => "/login",
}));

const ME = { id: "00000000-0000-4000-8000-000000000001", email: "fixture@example.test", created_at: "2026-01-01T00:00:00Z" };

function renderWithAuth(ui: React.ReactNode) {
  return render(<AuthProvider>{ui}</AuthProvider>);
}

beforeEach(() => replace.mockReset());

describe("login form", () => {
  it("validates before sending anything", async () => {
    const { calls } = mockFetch(() => errorResponse(401, "no_session", "Not signed in."));
    renderWithAuth(<LoginForm />);
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(screen.getByText("Enter a valid email address.")).toBeInTheDocument();
    expect(screen.getByText("Enter your password.")).toBeInTheDocument();
    expect(screen.getByLabelText("Email")).toHaveAttribute("aria-invalid", "true");
    // Only the session-restore attempt on mount.
    expect(calls.map((c) => c.url)).toEqual(["/bff/auth/refresh"]);
  });

  it("shows one message for wrong credentials and clears the password", async () => {
    mockFetch((url) =>
      url === "/bff/auth/login"
        ? errorResponse(401, "invalid_credentials", "Invalid email or password.")
        : errorResponse(401, "no_session", "Not signed in."),
    );
    renderWithAuth(<LoginForm />);
    await userEvent.type(screen.getByLabelText("Email"), "fixture@example.test");
    await userEvent.type(screen.getByLabelText("Password"), "wrong-fixture-password");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Incorrect email or password.");
    expect(screen.getByLabelText("Password")).toHaveValue("");
  });

  it("shows the wait time when rate limited", async () => {
    mockFetch((url) =>
      url === "/bff/auth/login"
        ? errorResponse(429, "rate_limited", "Too many requests.", { "Retry-After": "120" })
        : errorResponse(401, "no_session", "Not signed in."),
    );
    renderWithAuth(<LoginForm />);
    await userEvent.type(screen.getByLabelText("Email"), "fixture@example.test");
    await userEvent.type(screen.getByLabelText("Password"), "fixture-password");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Try again in 2 minutes.");
  });

  it("signs in and goes to the requested page", async () => {
    mockFetch((url) => {
      if (url === "/bff/auth/login") return jsonResponse(FIXTURE_TOKEN);
      if (url === "/bff/api/auth/me") return jsonResponse(ME);
      return errorResponse(401, "no_session", "Not signed in.");
    });
    renderWithAuth(<LoginForm />);
    await userEvent.type(screen.getByLabelText("Email"), "fixture@example.test");
    await userEvent.type(screen.getByLabelText("Password"), "fixture-password");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    await vi.waitFor(() => expect(replace).toHaveBeenCalledWith("/documents"));
  });
});

describe("register form", () => {
  it("enforces the password policy and the confirmation", async () => {
    mockFetch(() => errorResponse(401, "no_session", "Not signed in."));
    renderWithAuth(<RegisterForm />);
    await userEvent.type(screen.getByLabelText("Email"), "fixture@example.test");
    await userEvent.type(screen.getByLabelText("Password"), "short");
    await userEvent.type(screen.getByLabelText("Confirm password"), "different");
    await userEvent.click(screen.getByRole("button", { name: "Create account" }));
    expect(screen.getByText("Use at least 12 characters.")).toBeInTheDocument();
    expect(screen.getByText("The passwords do not match.")).toBeInTheDocument();
  });

  it("explains an already registered email", async () => {
    mockFetch((url) =>
      url === "/bff/api/auth/register"
        ? errorResponse(409, "email_already_registered", "An account with this email already exists.")
        : errorResponse(401, "no_session", "Not signed in."),
    );
    renderWithAuth(<RegisterForm />);
    await userEvent.type(screen.getByLabelText("Email"), "fixture@example.test");
    await userEvent.type(screen.getByLabelText("Password"), "fixture-password-long");
    await userEvent.type(screen.getByLabelText("Confirm password"), "fixture-password-long");
    await userEvent.click(screen.getByRole("button", { name: "Create account" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("already exists. Sign in instead.");
  });
});
