"use client";

import { useMemo } from "react";
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
import type { Bucket, Codebook, ResponseCode } from "@/lib/types";
import { Button, DragHandle } from "./ui";

let uid = 0;
const genBucketId = () =>
  `b_${Date.now().toString(36)}_${(uid++).toString(36)}`;

export function BucketEditor({
  codebook,
  column,
  buckets,
  onChange,
}: {
  codebook: Codebook;
  column: string | null;
  buckets: Bucket[];
  onChange: (buckets: Bucket[]) => void;
}) {
  const col = column ? codebook.columns[column] : null;
  const options = col?.options ?? [];

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 4 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  );

  const bucketIds = useMemo(() => buckets.map((b) => b.id), [buckets]);

  function handleDragEnd(ev: DragEndEvent) {
    const { active, over } = ev;
    if (!over || active.id === over.id) return;
    const oldIndex = buckets.findIndex((b) => b.id === active.id);
    const newIndex = buckets.findIndex((b) => b.id === over.id);
    if (oldIndex < 0 || newIndex < 0) return;
    onChange(arrayMove(buckets, oldIndex, newIndex));
  }

  function updateBucket(id: string, patch: Partial<Bucket>) {
    onChange(buckets.map((b) => (b.id === id ? { ...b, ...patch } : b)));
  }
  function toggleCode(b: Bucket, code: ResponseCode) {
    const has = b.codes.some((c) => String(c) === String(code));
    const next = has
      ? b.codes.filter((c) => String(c) !== String(code))
      : [...b.codes, code];
    updateBucket(b.id, { codes: next });
  }
  function removeBucket(id: string) {
    onChange(buckets.filter((b) => b.id !== id));
  }
  function addBucket() {
    onChange([
      ...buckets,
      {
        id: genBucketId(),
        name: `Bucket ${buckets.length + 1}`,
        codes: [],
      },
    ]);
  }

  if (!column) return null;

  return (
    <div className="space-y-2">
      <DndContext
        sensors={sensors}
        collisionDetection={closestCenter}
        onDragEnd={handleDragEnd}
      >
        <SortableContext items={bucketIds} strategy={verticalListSortingStrategy}>
          {buckets.map((b) => (
            <SortableBucketRow
              key={b.id}
              bucket={b}
              options={options}
              onUpdate={(patch) => updateBucket(b.id, patch)}
              onToggleCode={(code) => toggleCode(b, code)}
              onRemove={() => removeBucket(b.id)}
            />
          ))}
        </SortableContext>
      </DndContext>
      <Button variant="secondary" onClick={addBucket}>
        + Add Bucket
      </Button>
    </div>
  );
}

function SortableBucketRow({
  bucket,
  options,
  onUpdate,
  onToggleCode,
  onRemove,
}: {
  bucket: Bucket;
  options: { code: ResponseCode; label: string }[];
  onUpdate: (patch: Partial<Bucket>) => void;
  onToggleCode: (code: ResponseCode) => void;
  onRemove: () => void;
}) {
  const {
    attributes,
    listeners,
    setNodeRef,
    transform,
    transition,
    isDragging,
  } = useSortable({ id: bucket.id });

  const style: React.CSSProperties = {
    transform: CSS.Transform.toString(transform),
    transition,
  };

  return (
    <div
      ref={setNodeRef}
      style={style}
      className={`flex flex-wrap items-center gap-2 border border-[color:var(--border)] rounded-md px-2.5 py-2 bg-white ${
        isDragging ? "sortable-dragging shadow-lg" : ""
      }`}
    >
      <DragHandle listeners={listeners} attributes={attributes} />
      <input
        type="text"
        value={bucket.name}
        onChange={(e) => onUpdate({ name: e.target.value })}
        className="flex-1 min-w-32 max-w-64 border border-[color:var(--border)] rounded px-2 py-1 text-sm"
      />
      <div className="flex flex-wrap gap-3">
        {options.map((opt) => (
          <label
            key={String(opt.code)}
            className="flex items-center gap-1 text-xs"
          >
            <input
              type="checkbox"
              checked={bucket.codes.some(
                (c) => String(c) === String(opt.code),
              )}
              onChange={() => onToggleCode(opt.code)}
              className="accent-[color:var(--accent)]"
            />
            <span>{opt.label}</span>
          </label>
        ))}
      </div>
      <Button
        variant="ghost"
        className="!px-2 !py-1 text-xs text-[color:var(--danger)]"
        onClick={onRemove}
        title="Remove bucket"
      >
        ×
      </Button>
    </div>
  );
}

export { genBucketId };
