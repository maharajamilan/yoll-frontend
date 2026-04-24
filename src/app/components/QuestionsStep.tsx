"use client";

import {
  DndContext,
  closestCenter,
  KeyboardSensor,
  PointerSensor,
  useSensor,
  useSensors,
  type DragEndEvent,
} from "@dnd-kit/core";
import {
  arrayMove,
  SortableContext,
  sortableKeyboardCoordinates,
  useSortable,
  verticalListSortingStrategy,
} from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import { useMemo, useState } from "react";
import type { Codebook, Question } from "@/lib/types";
import { defaultBucketsForColumn } from "@/lib/crosstab";
import { Button, DragHandle } from "./ui";
import { ColumnCombobox } from "./ColumnCombobox";
import { BucketEditor } from "./BucketEditor";

let uid = 0;
const genId = () =>
  `q_${Date.now().toString(36)}_${(uid++).toString(36)}`;

export function QuestionsStep({
  codebook,
  questions,
  onUpdate,
}: {
  codebook: Codebook;
  questions: Question[];
  onUpdate: (qs: Question[]) => void;
}) {
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 4 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  );
  const qIds = useMemo(() => questions.map((q) => q.id), [questions]);

  function addQuestion(col: string | null) {
    if (!col) return;
    if (questions.some((q) => q.column === col)) return;
    onUpdate([...questions, { id: genId(), column: col, responseBuckets: null }]);
  }
  function update(id: string, patch: Partial<Question>) {
    onUpdate(questions.map((q) => (q.id === id ? { ...q, ...patch } : q)));
  }
  function remove(id: string) {
    onUpdate(questions.filter((q) => q.id !== id));
  }
  function handleDragEnd(ev: DragEndEvent) {
    const { active, over } = ev;
    if (!over || active.id === over.id) return;
    const oldIndex = questions.findIndex((q) => q.id === active.id);
    const newIndex = questions.findIndex((q) => q.id === over.id);
    if (oldIndex < 0 || newIndex < 0) return;
    onUpdate(arrayMove(questions, oldIndex, newIndex));
  }

  return (
    <div className="space-y-3">
      <div className="flex gap-2">
        <ColumnCombobox
          codebook={codebook}
          value={null}
          onChange={addQuestion}
          placeholder="Search columns to analyze..."
          excludeColumns={questions.map((q) => q.column)}
        />
      </div>

      <DndContext
        sensors={sensors}
        collisionDetection={closestCenter}
        onDragEnd={handleDragEnd}
      >
        <SortableContext items={qIds} strategy={verticalListSortingStrategy}>
          <ul className="space-y-1.5">
            {questions.map((q) => (
              <SortableQuestionRow
                key={q.id}
                question={q}
                codebook={codebook}
                onUpdate={(patch) => update(q.id, patch)}
                onRemove={() => remove(q.id)}
              />
            ))}
          </ul>
        </SortableContext>
      </DndContext>
    </div>
  );
}

function SortableQuestionRow({
  question,
  codebook,
  onUpdate,
  onRemove,
}: {
  question: Question;
  codebook: Codebook;
  onUpdate: (patch: Partial<Question>) => void;
  onRemove: () => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const {
    attributes,
    listeners,
    setNodeRef,
    transform,
    transition,
    isDragging,
  } = useSortable({ id: question.id });

  const style: React.CSSProperties = {
    transform: CSS.Transform.toString(transform),
    transition,
  };

  const def = codebook.columns[question.column];
  const hasCustomBuckets =
    question.responseBuckets !== null && question.responseBuckets.length > 0;

  function enableCustomBuckets() {
    const buckets = defaultBucketsForColumn(codebook, question.column, `rq_${question.id}`);
    onUpdate({ responseBuckets: buckets });
    setExpanded(true);
  }
  function resetToDefault() {
    onUpdate({ responseBuckets: null });
  }

  return (
    <li
      ref={setNodeRef}
      style={style}
      className={`border border-[color:var(--border)] rounded-md bg-white ${
        isDragging ? "sortable-dragging shadow-lg" : ""
      }`}
    >
      <div className="flex items-center gap-2 px-3 py-2">
        <DragHandle listeners={listeners} attributes={attributes} />
        <div
          className="flex-1 min-w-0 cursor-pointer"
          onClick={() => setExpanded((e) => !e)}
        >
          <div className="font-medium text-sm flex items-center gap-2">
            <span>{def?.label ?? question.column}</span>
            {hasCustomBuckets && (
              <span className="text-[10px] uppercase tracking-wide text-[color:var(--accent)] bg-[color:var(--accent-soft)] px-1.5 py-0.5 rounded">
                Custom rows
              </span>
            )}
          </div>
          {def?.question && def.question !== def.label && (
            <div className="text-xs text-[color:var(--muted)] truncate">
              {def.question}
            </div>
          )}
        </div>
        <Button
          variant="ghost"
          className="!px-2 !py-1 text-xs"
          onClick={() => setExpanded((e) => !e)}
        >
          {expanded ? "Collapse" : "Edit rows"}
        </Button>
        <Button
          variant="ghost"
          className="!px-2 !py-1 text-xs text-[color:var(--danger)]"
          onClick={onRemove}
        >
          Remove
        </Button>
      </div>

      {expanded && (
        <div className="border-t border-[color:var(--border)] px-3 py-3 bg-[color:var(--stripe)]/40 space-y-3">
          {!hasCustomBuckets ? (
            <div className="flex items-center justify-between gap-4 flex-wrap">
              <p className="text-xs text-[color:var(--muted)]">
                Using the codebook&apos;s response options as-is. Combine or
                rename responses (e.g., merge &quot;Strongly approve&quot; and
                &quot;Somewhat approve&quot; into &quot;Approve&quot;) by
                customizing row buckets.
              </p>
              <Button variant="secondary" onClick={enableCustomBuckets}>
                Customize rows
              </Button>
            </div>
          ) : (
            <>
              <div className="flex items-center justify-between">
                <p className="text-xs text-[color:var(--muted)]">
                  Each row bucket can contain one or more response codes. Drag
                  to reorder.
                </p>
                <Button variant="ghost" className="!px-2 !py-1 text-xs" onClick={resetToDefault}>
                  Reset to default
                </Button>
              </div>
              <BucketEditor
                codebook={codebook}
                column={question.column}
                buckets={question.responseBuckets!}
                onChange={(buckets) => onUpdate({ responseBuckets: buckets })}
              />
            </>
          )}
        </div>
      )}
    </li>
  );
}
