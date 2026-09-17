import type { ReactNode } from "react";
import { Mark } from "../lib/mark";

export function EmptyState({
  title,
  body,
  children,
}: {
  title: string;
  body?: string | null;
  children?: ReactNode;
}) {
  return (
    <div className="card">
      <div className="empty">
        <div className="icon markempty">
          <Mark size={30} />
        </div>
        <h3>{title}</h3>
        {body ? <p>{body}</p> : null}
        {children}
      </div>
    </div>
  );
}

export function Banner({
  children,
  kind,
}: {
  children: ReactNode;
  kind?: "info" | "warn";
}) {
  const cls =
    kind === "warn" ? "banner warnbar" : kind === "info" ? "banner info" : "banner";
  return <div className={cls}>{children}</div>;
}

export function Loading({ message, inline }: { message: string; inline?: boolean }) {
  return <div className={inline ? "loading inline" : "loading"}>{message}</div>;
}
