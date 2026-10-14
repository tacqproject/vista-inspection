import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { CheckCircle2, XCircle, Send, RotateCcw, Share2, Copy, Check, ShieldCheck } from 'lucide-react'
import { inspectionsApi } from '../api/inspections'
import { useAuthStore } from '../store/authStore'
import type { Inspection } from '../types'

const STATUS: Record<string, { label: string; cls: string }> = {
  draft: { label: 'Koncept', cls: 'bg-gray-100 text-gray-600' },
  submitted: { label: 'Ke schválení', cls: 'bg-amber-100 text-amber-800' },
  approved: { label: 'Schváleno', cls: 'bg-green-100 text-green-800' },
  rejected: { label: 'Zamítnuto', cls: 'bg-red-100 text-red-800' },
}

/** Report approval workflow + read-only share link for an inspection. */
export default function ApprovalControls({ structureId, inspection }: {
  structureId: string
  inspection: Inspection
}) {
  const qc = useQueryClient()
  const canEdit = useAuthStore(s => s.canEdit())
  const isAdmin = useAuthStore(s => s.isAdmin())
  const [copied, setCopied] = useState(false)

  const status = inspection.approval_status ?? 'draft'
  const meta = STATUS[status] ?? STATUS.draft
  const invalidate = () => qc.invalidateQueries({ queryKey: ['inspection', structureId, inspection.id] })

  const approvalMut = useMutation({
    mutationFn: (action: 'submit' | 'approve' | 'reject' | 'reopen') =>
      inspectionsApi.setApproval(structureId, inspection.id, action),
    onSuccess: invalidate,
  })
  const shareMut = useMutation({
    mutationFn: () => inspectionsApi.createShare(structureId, inspection.id),
    onSuccess: invalidate,
  })
  const revokeMut = useMutation({
    mutationFn: () => inspectionsApi.revokeShare(structureId, inspection.id),
    onSuccess: invalidate,
  })

  const shareUrl = inspection.share_token
    ? `${window.location.origin}/api/reports/shared/${inspection.share_token}`
    : null
  const copy = () => {
    if (!shareUrl) return
    navigator.clipboard.writeText(shareUrl)
    setCopied(true)
    setTimeout(() => setCopied(false), 1600)
  }

  return (
    <div className="flex items-center gap-2 flex-wrap">
      <span className={`badge ${meta.cls} flex items-center gap-1`}>
        {status === 'approved' && <ShieldCheck size={12} />}
        {meta.label}
      </span>
      {status === 'approved' && inspection.approved_by && (
        <span className="text-[11px] text-gray-400">
          {inspection.approved_by}{inspection.approved_at ? ` · ${inspection.approved_at.slice(0, 10)}` : ''}
        </span>
      )}

      {canEdit && (status === 'draft' || status === 'rejected') && (
        <button onClick={() => approvalMut.mutate('submit')} disabled={approvalMut.isPending}
          className="btn-secondary text-xs">
          <Send size={13} /> Odeslat ke schválení
        </button>
      )}
      {isAdmin && status === 'submitted' && (
        <>
          <button onClick={() => approvalMut.mutate('approve')} disabled={approvalMut.isPending}
            className="btn-primary text-xs">
            <CheckCircle2 size={13} /> Schválit
          </button>
          <button onClick={() => approvalMut.mutate('reject')} disabled={approvalMut.isPending}
            className="btn-ghost text-xs text-red-500">
            <XCircle size={13} /> Zamítnout
          </button>
        </>
      )}
      {canEdit && status === 'approved' && (
        <button onClick={() => approvalMut.mutate('reopen')} disabled={approvalMut.isPending}
          className="btn-ghost text-xs text-gray-500">
          <RotateCcw size={13} /> Znovu otevřít
        </button>
      )}

      {/* Read-only share link */}
      {canEdit && (
        shareUrl ? (
          <div className="flex items-center gap-1">
            <button onClick={copy} className="btn-secondary text-xs" title={shareUrl}>
              {copied ? <Check size={13} className="text-green-600" /> : <Copy size={13} />}
              {copied ? 'Zkopírováno' : 'Kopírovat odkaz'}
            </button>
            <button onClick={() => revokeMut.mutate()} disabled={revokeMut.isPending}
              className="btn-ghost text-xs text-red-500">Zrušit sdílení</button>
          </div>
        ) : (
          <button onClick={() => shareMut.mutate()} disabled={shareMut.isPending}
            className="btn-ghost text-xs text-brand-600">
            <Share2 size={13} /> Sdílet protokol (jen ke čtení)
          </button>
        )
      )}
    </div>
  )
}
