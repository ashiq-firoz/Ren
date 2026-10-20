"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Check, Link2, MailPlus, Trash2, UserX, Users } from "lucide-react";
import { useState } from "react";
import { RoleBadge, ROLE_LABEL } from "@/components/domain/status";
import { PageContainer, PageHeader } from "@/components/shell/page-header";
import { Avatar } from "@/components/ui/avatar";
import { Badge } from "@/components/ui/badge";
import { Button, ButtonLink, IconButton } from "@/components/ui/button";
import { Card, CardHeader } from "@/components/ui/card";
import { CopyButton } from "@/components/ui/copy-button";
import { EmptyState, ErrorState, Notice, SkeletonRows } from "@/components/ui/feedback";
import { Field, Input, Select } from "@/components/ui/input";
import { confirm, Dialog } from "@/components/ui/overlay";
import { Table, TableContainer, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import { useInvites, useMembers } from "@/hooks/queries";
import { api } from "@/lib/api";
import { removeFromList, upsertInList } from "@/lib/cache";
import { formatDate, timeAgo, timeUntil } from "@/lib/format";
import { qk } from "@/lib/query-keys";
import { toast } from "@/lib/toast";
import type { Invite, ListResponse, Member, Role } from "@/lib/types";
import { useAuth, useRole } from "@/providers/auth";

const ASSIGNABLE: Role[] = ["viewer", "member", "admin", "owner"];

function InviteDialog({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient();
  const { isOwner } = useRole();
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<Role>("member");
  const [created, setCreated] = useState<Invite | null>(null);
  const invite = useMutation({
    mutationFn: () => api.post<Invite>("/orgs/current/invites", { email: email.trim(), role }),
    onSuccess: (inv) => {
      qc.setQueryData<ListResponse<Invite>>(qk.invites, (old) => (old ? upsertInList(old, inv, { prepend: true }) : old));
      setCreated(inv);
    },
    meta: { errorTitle: "Couldn't create the invite" },
  });
  const link = created ? created.link || `${window.location.origin}/invite/${created.token}` : "";
  return (
    <Dialog
      open
      onOpenChange={(o) => !o && onClose()}
      title={created ? "Invite ready" : "Invite a teammate"}
      description={created ? `Share this link with ${created.email}. It expires ${timeUntil(created.expires_at)}.` : "They'll get access to this workspace's agents, chat and knowledge according to their role."}
      footer={
        created ? (
          <>
            <Button
              variant="ghost"
              onClick={() => {
                setCreated(null);
                setEmail("");
              }}
            >
              Invite another
            </Button>
            <Button variant="primary" onClick={onClose}>
              Done
            </Button>
          </>
        ) : (
          <>
            <Button variant="ghost" onClick={onClose}>
              Cancel
            </Button>
            <Button variant="primary" icon={<MailPlus className="size-4" />} loading={invite.isPending} disabled={!/^\S+@\S+\.\S+$/.test(email.trim())} onClick={() => invite.mutate()}>
              Create invite
            </Button>
          </>
        )
      }
    >
      {created ? (
        <div className="space-y-3">
          <div className="flex items-center gap-2 rounded-control border border-border bg-[var(--code-bg)] px-3 py-2">
            <Link2 className="size-4 shrink-0 text-muted" />
            <code className="min-w-0 flex-1 truncate font-mono text-[12.5px] text-fg">{link}</code>
            <CopyButton text={link} label="Copy invite link" showLabel />
          </div>
          <Notice tone="neutral" icon={<Check />}>
            {created.email} joins as <span className="font-medium text-fg">{ROLE_LABEL[created.role]}</span>.
          </Notice>
        </div>
      ) : (
        <form
          className="space-y-4"
          onSubmit={(e) => {
            e.preventDefault();
            invite.mutate();
          }}
        >
          <Field label="Email" required>
            <Input data-autofocus type="email" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="teammate@company.com" />
          </Field>
          <Field label="Role" hint="Viewers can chat; members run tasks and upload; admins manage agents, policies and connectors.">
            <Select value={role} onChange={(e) => setRole(e.target.value as Role)}>
              {ASSIGNABLE.filter((r) => r !== "owner" || isOwner).map((r) => (
                <option key={r} value={r}>
                  {ROLE_LABEL[r]}
                </option>
              ))}
            </Select>
          </Field>
        </form>
      )}
    </Dialog>
  );
}

function MembersTable() {
  const qc = useQueryClient();
  const { user } = useAuth();
  const { isAdmin, isOwner } = useRole();
  const { data, isLoading, error, refetch } = useMembers();
  const [editingTitle, setEditingTitle] = useState<{ id: string; value: string } | null>(null);

  const update = useMutation({
    mutationFn: ({ id, patch }: { id: string; patch: { role?: Role; title?: string } }) => api.patch<Member>(`/orgs/current/members/${id}`, patch),
    onSuccess: (m) => {
      qc.setQueryData<ListResponse<Member>>(qk.members, (old) =>
        old ? { ...old, items: old.items.map((x) => (x.user_id === m.user_id ? m : x)) } : old,
      );
      toast.success("Member updated", { description: m.name });
    },
    meta: { errorTitle: "Couldn't update the member" },
  });
  const remove = useMutation({
    mutationFn: (m: Member) => api.delete(`/orgs/current/members/${m.user_id}`),
    onSuccess: (_d, m) => {
      qc.setQueryData<ListResponse<Member>>(qk.members, (old) =>
        old ? { items: old.items.filter((x) => x.user_id !== m.user_id), total: Math.max(0, old.total - 1) } : old,
      );
      toast.success("Member removed", { description: m.name });
    },
    meta: { errorTitle: "Couldn't remove the member" },
  });

  if (isLoading) return <SkeletonRows rows={5} />;
  if (error && !data) return <ErrorState error={error} onRetry={() => refetch()} />;
  const items = data?.items ?? [];
  if (items.length === 0) return <EmptyState compact icon={<Users />} title="No members" />;
  return (
    <TableContainer>
      <Table>
        <THead>
          <tr>
            <TH>Member</TH>
            <TH>Title</TH>
            <TH>Role</TH>
            <TH>Joined</TH>
            <TH>Last active</TH>
            <TH className="w-10">
              <span className="sr-only">Actions</span>
            </TH>
          </tr>
        </THead>
        <TBody>
          {items.map((m) => {
            const self = m.user_id === user?.id;
            const canEdit = isAdmin && (m.role !== "owner" || isOwner);
            return (
              <TR key={m.user_id}>
                <TD>
                  <span className="flex items-center gap-3">
                    <Avatar name={m.name} size="md" />
                    <span className="min-w-0">
                      <span className="flex items-center gap-2 truncate font-medium text-fg">
                        {m.name}
                        {self && (
                          <Badge tone="outline" size="xs">
                            You
                          </Badge>
                        )}
                      </span>
                      <span className="block truncate text-xs text-muted">{m.email}</span>
                    </span>
                  </span>
                </TD>
                <TD className="min-w-[160px]">
                  {editingTitle?.id === m.user_id ? (
                    <form
                      onSubmit={(e) => {
                        e.preventDefault();
                        update.mutate({ id: m.user_id, patch: { title: editingTitle.value.trim() } });
                        setEditingTitle(null);
                      }}
                    >
                      <Input
                        inputSize="sm"
                        autoFocus
                        value={editingTitle.value}
                        onChange={(e) => setEditingTitle({ id: m.user_id, value: e.target.value })}
                        onBlur={() => setEditingTitle(null)}
                        onKeyDown={(e) => e.key === "Escape" && setEditingTitle(null)}
                        aria-label="Title"
                      />
                    </form>
                  ) : canEdit ? (
                    <button
                      type="button"
                      onClick={() => setEditingTitle({ id: m.user_id, value: m.title ?? "" })}
                      className="rounded px-1 text-left text-muted hover:bg-surface-2 hover:text-fg"
                      title="Edit title"
                    >
                      {m.title || <span className="text-subtle">Add title</span>}
                    </button>
                  ) : (
                    <span className="text-muted">{m.title || "—"}</span>
                  )}
                </TD>
                <TD>
                  {canEdit ? (
                    <Select
                      selectSize="sm"
                      value={m.role}
                      onChange={(e) => update.mutate({ id: m.user_id, patch: { role: e.target.value as Role } })}
                      aria-label={`Role for ${m.name}`}
                      wrapperClassName="w-32"
                    >
                      {ASSIGNABLE.filter((r) => r !== "owner" || isOwner).map((r) => (
                        <option key={r} value={r}>
                          {ROLE_LABEL[r]}
                        </option>
                      ))}
                    </Select>
                  ) : (
                    <RoleBadge role={m.role} />
                  )}
                </TD>
                <TD className="whitespace-nowrap text-muted">{formatDate(m.joined_at)}</TD>
                <TD className="whitespace-nowrap text-muted">{m.last_active_at ? timeAgo(m.last_active_at) : "—"}</TD>
                <TD>
                  {canEdit && !self && (
                    <IconButton
                      label={`Remove ${m.name}`}
                      icon={<UserX />}
                      variant="danger"
                      onClick={async () => {
                        const ok = await confirm({
                          title: `Remove ${m.name}?`,
                          description: "They lose access to this workspace immediately. Their conversations and tasks are kept.",
                          confirmLabel: "Remove member",
                          danger: true,
                        });
                        if (ok) remove.mutate(m);
                      }}
                    />
                  )}
                </TD>
              </TR>
            );
          })}
        </TBody>
      </Table>
    </TableContainer>
  );
}

function InvitesCard() {
  const qc = useQueryClient();
  const { data, isLoading, error, refetch } = useInvites();
  const revoke = useMutation({
    mutationFn: (inv: Invite) => api.delete(`/orgs/current/invites/${inv.id}`),
    onSuccess: (_d, inv) => {
      qc.setQueryData<ListResponse<Invite>>(qk.invites, (old) => (old ? removeFromList(old, inv.id) : old));
      toast.success("Invite revoked", { description: inv.email });
    },
    meta: { errorTitle: "Couldn't revoke the invite" },
  });
  const pending = (data?.items ?? []).filter((i) => !i.accepted);
  return (
    <Card className="overflow-hidden">
      <CardHeader title="Pending invites" description="Links stay valid until accepted, revoked or expired." />
      {isLoading ? (
        <SkeletonRows rows={2} />
      ) : error && !data ? (
        <ErrorState compact error={error} onRetry={() => refetch()} />
      ) : pending.length === 0 ? (
        <p className="px-4 py-6 text-center text-xs text-muted">No pending invites.</p>
      ) : (
        <ul className="divide-y divide-border">
          {pending.map((inv) => {
            const link = inv.link || (typeof window !== "undefined" ? `${window.location.origin}/invite/${inv.token}` : "");
            return (
              <li key={inv.id} className="flex flex-wrap items-center gap-3 px-4 py-3">
                <div className="min-w-0 flex-1">
                  <p className="truncate text-[13px] font-medium text-fg">{inv.email}</p>
                  <p className="text-xs text-muted">
                    {ROLE_LABEL[inv.role]} · invited {timeAgo(inv.created_at)}
                    {inv.invited_by_name ? ` by ${inv.invited_by_name}` : ""} · expires {timeUntil(inv.expires_at)}
                  </p>
                </div>
                <CopyButton text={link} label="Copy link" showLabel />
                <IconButton
                  label={`Revoke invite for ${inv.email}`}
                  icon={<Trash2 />}
                  variant="danger"
                  onClick={async () => {
                    const ok = await confirm({ title: `Revoke invite for ${inv.email}?`, confirmLabel: "Revoke", danger: true });
                    if (ok) revoke.mutate(inv);
                  }}
                />
              </li>
            );
          })}
        </ul>
      )}
    </Card>
  );
}

export default function TeamPage() {
  const { isAdmin, mode } = useRole();
  const [inviting, setInviting] = useState(false);

  if (mode === "personal") {
    return (
      <PageContainer>
        <PageHeader title="Team" />
        <Card>
          <EmptyState
            icon={<Users />}
            title="This is a personal workspace"
            description="Switch the workspace to Business mode in Settings to invite teammates, or create a separate business workspace."
            action={<ButtonLink href="/settings">Open settings</ButtonLink>}
          />
        </Card>
      </PageContainer>
    );
  }

  return (
    <PageContainer>
      <PageHeader
        title="Team"
        description="People in this workspace and what they can do."
        actions={
          isAdmin && (
            <Button variant="primary" icon={<MailPlus className="size-4" />} onClick={() => setInviting(true)}>
              Invite
            </Button>
          )
        }
      />
      <div className="space-y-5">
        <Card className="overflow-hidden">
          <CardHeader title="Members" />
          <MembersTable />
        </Card>
        {isAdmin && <InvitesCard />}
      </div>
      {inviting && <InviteDialog onClose={() => setInviting(false)} />}
    </PageContainer>
  );
}
