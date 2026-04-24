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
import { useMemo } from "react";
import type {
  Bucket,
  Codebook,
  Dimension,
  Group,
  Question,
} from "@/lib/types";
import { defaultBucketsForColumn } from "@/lib/crosstab";
import { Button, DragHandle } from "./ui";
import { ColumnCombobox } from "./ColumnCombobox";
import { BucketEditor } from "./BucketEditor";

let uid = 0;
const genId = (p: string) =>
  `${p}_${Date.now().toString(36)}_${(uid++).toString(36)}`;

export function newGroup(): Group {
  return {
    id: genId("g"),
    dimensions: [{ id: genId("d"), column: null, buckets: [] }],
  };
}

export function GroupsStep({
  codebook,
  groups,
  includeTotal,
  onToggleTotal,
  onUpdate,
}: {
  codebook: Codebook;
  groups: Group[];
  includeTotal: boolean;
  onToggleTotal: (v: boolean) => void;
  onUpdate: (groups: Group[]) => void;
}) {
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 4 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  );
  const groupIds = useMemo(() => groups.map((g) => g.id), [groups]);

  function handleDragEnd(ev: DragEndEvent) {
    const { active, over } = ev;
    if (!over || active.id === over.id) return;
    const oldIndex = groups.findIndex((g) => g.id === active.id);
    const newIndex = groups.findIndex((g) => g.id === over.id);
    if (oldIndex < 0 || newIndex < 0) return;
    onUpdate(arrayMove(groups, oldIndex, newIndex));
  }

  function updateGroup(id: string, patch: Partial<Group>) {
    onUpdate(groups.map((g) => (g.id === id ? { ...g, ...patch } : g)));
  }
  function removeGroup(id: string) {
    onUpdate(groups.filter((g) => g.id !== id));
  }
  function addGroup() {
    onUpdate([...groups, newGroup()]);
  }

  return (
    <div className="space-y-4">
      <label className="flex items-center gap-2 text-sm">
        <input
          type="checkbox"
          checked={includeTotal}
          onChange={(e) => onToggleTotal(e.target.checked)}
          className="accent-[color:var(--accent)]"
        />
        <span>Include &quot;Total&quot; (all respondents) column in results</span>
      </label>

      <DndContext
        sensors={sensors}
        collisionDetection={closestCenter}
        onDragEnd={handleDragEnd}
      >
        <SortableContext items={groupIds} strategy={verticalListSortingStrategy}>
          {groups.map((g) => (
            <SortableGroupCard
              key={g.id}
              group={g}
              codebook={codebook}
              onUpdate={(patch) => updateGroup(g.id, patch)}
              onRemove={() => removeGroup(g.id)}
            />
          ))}
        </SortableContext>
      </DndContext>

      <Button variant="secondary" onClick={addGroup}>
        + Add Group
      </Button>
    </div>
  );
}

function SortableGroupCard({
  group,
  codebook,
  onUpdate,
  onRemove,
}: {
  group: Group;
  codebook: Codebook;
  onUpdate: (patch: Partial<Group>) => void;
  onRemove: () => void;
}) {
  const {
    attributes,
    listeners,
    setNodeRef,
    transform,
    transition,
    isDragging,
  } = useSortable({ id: group.id });

  const style: React.CSSProperties = {
    transform: CSS.Transform.toString(transform),
    transition,
  };

  function updateDim(id: string, patch: Partial<Dimension>) {
    onUpdate({
      dimensions: group.dimensions.map((d) =>
        d.id === id ? { ...d, ...patch } : d,
      ),
    });
  }
  function setDimColumn(id: string, col: string | null) {
    const buckets = col ? defaultBucketsForColumn(codebook, col, `b_${id}`) : [];
    onUpdate({
      dimensions: group.dimensions.map((d) =>
        d.id === id ? { ...d, column: col, buckets } : d,
      ),
    });
  }
  function removeDim(id: string) {
    if (group.dimensions.length <= 1) return;
    onUpdate({ dimensions: group.dimensions.filter((d) => d.id !== id) });
  }
  function addDim() {
    onUpdate({
      dimensions: [
        ...group.dimensions,
        { id: genId("d"), column: null, buckets: [] },
      ],
    });
  }

  return (
    <div
      ref={setNodeRef}
      style={style}
      className={`border border-[color:var(--border)] rounded-lg p-4 bg-white ${
        isDragging ? "sortable-dragging shadow-lg" : ""
      }`}
    >
      <div className="flex items-center justify-between gap-2 mb-3">
        <DragHandle listeners={listeners} attributes={attributes} />
        <div className="flex-1" />
        <Button variant="danger" onClick={onRemove}>
          Remove Group
        </Button>
      </div>

      <div className="space-y-4">
        {group.dimensions.map((dim, i) => (
          <div
            key={dim.id}
            className={
              i > 0
                ? "pt-4 border-t border-dashed border-[color:var(--border)]"
                : ""
            }
          >
            <div className="flex items-center gap-2 mb-2">
              <span className="text-xs uppercase tracking-wide text-[color:var(--muted)] font-semibold">
                {i === 0 ? "Dimension" : `Subgroup ${i}`}
              </span>
              {i > 0 && (
                <Button
                  variant="ghost"
                  className="!px-2 !py-0.5 text-xs text-[color:var(--danger)]"
                  onClick={() => removeDim(dim.id)}
                >
                  Remove
                </Button>
              )}
            </div>
            <div className="flex gap-2 mb-3">
              <ColumnCombobox
                codebook={codebook}
                value={dim.column}
                onChange={(col) => setDimColumn(dim.id, col)}
                placeholder="Pick a dimension (e.g. Age, Race, Party ID)"
              />
            </div>
            {dim.column && (
              <BucketEditor
                codebook={codebook}
                column={dim.column}
                buckets={dim.buckets}
                onChange={(buckets) => updateDim(dim.id, { buckets })}
              />
            )}
          </div>
        ))}
      </div>

      <div className="mt-4">
        <Button variant="secondary" onClick={addDim}>
          + Add Subgroup Dimension
        </Button>
      </div>
    </div>
  );
}

/**
 * Re-exported so code that wants to seed question-level response buckets can
 * use the same default-bucketing logic.
 */
export { defaultBucketsForColumn };
export type { Bucket, Question };
