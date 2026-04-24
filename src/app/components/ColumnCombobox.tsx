"use client";

import { useMemo, useRef, useState, useEffect } from "react";
import type { Codebook } from "@/lib/types";

type Option = {
  column: string;
  label: string;
  question: string;
};

export function ColumnCombobox({
  codebook,
  value,
  onChange,
  placeholder = "Search columns...",
  filter,
  excludeColumns = [],
}: {
  codebook: Codebook;
  value: string | null;
  onChange: (column: string | null) => void;
  placeholder?: string;
  filter?: (col: string) => boolean;
  excludeColumns?: string[];
}) {
  const [query, setQuery] = useState(value ?? "");
  const [open, setOpen] = useState(false);
  const [highlight, setHighlight] = useState(0);
  const containerRef = useRef<HTMLDivElement>(null);

  // If outside value changes (e.g. reset), reflect it in the text.
  useEffect(() => {
    setQuery(value ?? "");
  }, [value]);

  const allOptions: Option[] = useMemo(() => {
    return Object.entries(codebook.columns)
      .filter(([col]) => !excludeColumns.includes(col))
      .filter(([col]) => (filter ? filter(col) : true))
      .map(([col, def]) => ({
        column: col,
        label: def.label,
        question: def.question,
      }));
  }, [codebook, filter, excludeColumns]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return allOptions.slice(0, 200);
    return allOptions
      .filter(
        (o) =>
          o.column.toLowerCase().includes(q) ||
          o.question.toLowerCase().includes(q),
      )
      .slice(0, 200);
  }, [allOptions, query]);

  useEffect(() => {
    function onDocClick(e: MouseEvent) {
      if (
        containerRef.current &&
        !containerRef.current.contains(e.target as Node)
      ) {
        setOpen(false);
      }
    }
    document.addEventListener("mousedown", onDocClick);
    return () => document.removeEventListener("mousedown", onDocClick);
  }, []);

  function commit(option: Option) {
    onChange(option.column);
    setQuery(option.column);
    setOpen(false);
  }

  return (
    <div className="relative flex-1" ref={containerRef}>
      <input
        type="text"
        value={query}
        placeholder={placeholder}
        onChange={(e) => {
          setQuery(e.target.value);
          setOpen(true);
          setHighlight(0);
          if (e.target.value === "") onChange(null);
        }}
        onFocus={() => setOpen(true)}
        onKeyDown={(e) => {
          if (e.key === "ArrowDown") {
            e.preventDefault();
            setHighlight((h) => Math.min(h + 1, filtered.length - 1));
          } else if (e.key === "ArrowUp") {
            e.preventDefault();
            setHighlight((h) => Math.max(h - 1, 0));
          } else if (e.key === "Enter") {
            if (filtered[highlight]) {
              e.preventDefault();
              commit(filtered[highlight]);
            }
          } else if (e.key === "Escape") {
            setOpen(false);
          }
        }}
        className="w-full border border-[color:var(--border)] rounded-md bg-white px-3 py-2 text-sm outline-none focus:border-ink"
      />
      {open && filtered.length > 0 && (
        <ul className="absolute z-20 mt-1 w-full max-h-72 overflow-auto bg-white border border-[color:var(--border)] rounded-md shadow-lg text-sm">
          {filtered.map((opt, i) => (
            <li
              key={opt.column}
              onMouseDown={(e) => {
                e.preventDefault();
                commit(opt);
              }}
              onMouseEnter={() => setHighlight(i)}
              className={`px-3 py-2 cursor-pointer ${
                i === highlight ? "bg-[color:var(--border)]/40" : ""
              }`}
            >
              <span className="font-medium">{opt.label}</span>
              {opt.question && opt.question !== opt.label && (
                <span className="text-muted"> — {truncate(opt.question, 80)}</span>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function truncate(s: string, n: number) {
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}
