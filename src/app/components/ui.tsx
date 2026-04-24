"use client";

import { ReactNode } from "react";

export function StepCard({
  number,
  title,
  children,
  disabled,
}: {
  number: number;
  title: string;
  children: ReactNode;
  disabled?: boolean;
}) {
  return (
    <section
      className={`rounded-xl bg-card border border-[color:var(--border)] shadow-sm p-6 md:p-7 ${
        disabled ? "opacity-50 pointer-events-none" : ""
      }`}
    >
      <h2 className="text-[15px] font-semibold mb-4 text-ink">
        {number}. {title}
      </h2>
      {children}
    </section>
  );
}

export function Button({
  onClick,
  children,
  variant = "primary",
  disabled,
  type = "button",
  className = "",
  title,
}: {
  onClick?: () => void;
  children: ReactNode;
  variant?: "primary" | "secondary" | "danger" | "ghost";
  disabled?: boolean;
  type?: "button" | "submit";
  className?: string;
  title?: string;
}) {
  const base =
    "inline-flex items-center gap-1.5 text-sm font-medium px-3.5 py-2 rounded-md transition-colors disabled:opacity-50 disabled:cursor-not-allowed";
  const variants = {
    primary:
      "bg-[color:var(--accent)] text-white hover:bg-[color:var(--ink-2)]",
    secondary:
      "bg-white text-ink border border-[color:var(--border)] hover:bg-[color:var(--stripe)]",
    danger:
      "bg-white text-[color:var(--danger)] border border-[color:var(--border)] hover:border-[color:var(--danger)] hover:bg-[color:var(--danger)]/5",
    ghost:
      "bg-transparent text-ink hover:bg-[color:var(--stripe)]",
  } as const;
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      title={title}
      className={`${base} ${variants[variant]} ${className}`}
    >
      {children}
    </button>
  );
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type AnyProps = Record<string, any>;

export function DragHandle({
  listeners,
  attributes,
}: {
  listeners?: AnyProps;
  attributes?: AnyProps;
}) {
  return (
    <span
      className="drag-handle flex items-center justify-center w-5 h-5 text-[color:var(--muted)]"
      {...(listeners ?? {})}
      {...(attributes ?? {})}
      aria-label="Drag to reorder"
    >
      <svg width="10" height="14" viewBox="0 0 10 14" fill="currentColor">
        <circle cx="2" cy="2" r="1.4" />
        <circle cx="8" cy="2" r="1.4" />
        <circle cx="2" cy="7" r="1.4" />
        <circle cx="8" cy="7" r="1.4" />
        <circle cx="2" cy="12" r="1.4" />
        <circle cx="8" cy="12" r="1.4" />
      </svg>
    </span>
  );
}
