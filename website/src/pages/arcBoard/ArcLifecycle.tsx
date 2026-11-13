import { useMutation, useQueryClient } from '@tanstack/react-query'

import { api } from '../../api/client'
import { arcBoardListKey, arcBoardQueryKey, type ArcBoardArc } from '../../api/client/arcBoard'
import { useConfirm } from '../../components/ConfirmDialog'
import ErrorNotice from '../../components/ErrorNotice'
import { Btn, Card, CardTitle } from '../../components/ui'
import { i18nT } from '../../i18n/t'
import { errMessage } from '../../utils/thunkError'

export default function ArcLifecycle({ arc }: { arc: ArcBoardArc }) {
  const client = useQueryClient()
  const { confirm, confirmDialog, confirmOpen } = useConfirm()
  const close = useMutation({
    mutationFn: () => api.arcBoardClose(arc.id),
    onSuccess: ({ arc: closedArc }) => {
      client.setQueryData(arcBoardQueryKey(arc.id), (data: { arc: ArcBoardArc } | undefined) =>
        data ? { ...data, arc: closedArc } : data,
      )
      void client.invalidateQueries({ queryKey: arcBoardListKey })
    },
  })

  const requestClose = async () => {
    if (
      await confirm({
        title: i18nT('pages.arcBoard.close_confirm', { name: arc.title }),
        body: i18nT('pages.arcBoard.close_hint'),
        confirmLabel: i18nT('pages.arcBoard.close_arc'),
        danger: false,
        primary: true,
      })
    ) {
      close.mutate()
    }
  }

  return (
    <Card className="p-3">
      <CardTitle>{i18nT('pages.arcBoard.lifecycle')}</CardTitle>
      <p className="mb-2 text-sm text-muted">{i18nT('pages.arcBoard.close_hint')}</p>
      <div aria-live="polite">
        <Btn
          type="button"
          disabled={!!arc.closed_at || close.isPending || confirmOpen}
          aria-busy={close.isPending}
          onClick={() => void requestClose()}
        >
          {i18nT(arc.closed_at ? 'pages.arcBoard.closed' : 'pages.arcBoard.close_arc')}
        </Btn>
      </div>
      {close.isError ? (
        <ErrorNotice message={errMessage(close.error) || i18nT('pages.arcBoard.close_failed')} askAgent />
      ) : null}
      {confirmDialog}
    </Card>
  )
}
