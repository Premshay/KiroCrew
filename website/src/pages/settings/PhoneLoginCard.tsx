import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { Copy, Smartphone } from 'lucide-react'
import { api } from '../../api/client'
import { Btn, Card, CardTitle, Input } from '../../components/ui'

import { i18nT } from '../../i18n/t'

export function PhoneLoginCard() {
  const [link, setLink] = useState('')
  const [copied, setCopied] = useState(false)
  const [copyFailed, setCopyFailed] = useState(false)

  const createLink = useMutation({
    mutationFn: api.phoneLoginLink,
    onMutate: () => {
      setCopied(false)
      setCopyFailed(false)
    },
    onSuccess: result => {
      setLink(result.url)
    },
  })

  const copyLink = async () => {
    if (!link) return
    try {
      await navigator.clipboard.writeText(link)
      setCopied(true)
      setCopyFailed(false)
    } catch {
      setCopied(false)
      setCopyFailed(true)
    }
  }

  return (
    <Card>
      <CardTitle>
        <Smartphone className="lucide-inline" aria-hidden="true" />
        {i18nT('pages.settings.phoneLoginCard.sign_in_on_phone')}
      </CardTitle>
      <p className="mt-2 text-sm leading-relaxed text-muted">
        {i18nT('pages.settings.phoneLoginCard.create_a_one_time_link_for_a_phone_or_another_br')}
      </p>
      {!link ? (
        <Btn className="mt-4" type="button" disabled={createLink.isPending} onClick={() => createLink.mutate()}>
          <Smartphone className="lucide-inline" aria-hidden="true" />
          {createLink.isPending
            ? i18nT('pages.settings.phoneLoginCard.creating_link')
            : i18nT('pages.settings.phoneLoginCard.create_phone_sign_in_link')}
        </Btn>
      ) : (
        <div className="mt-4">
          <label className="sr-only" htmlFor="phone-login-link">{i18nT('pages.settings.phoneLoginCard.phone_sign_in_link')}</label>
          <Input
            id="phone-login-link"
            className="w-full font-mono"
            readOnly
            value={link}
            onFocus={event => event.currentTarget.select()}
          />
          <div className="mt-3 flex flex-wrap items-center gap-3">
            <Btn type="button" onClick={() => void copyLink()}>
              <Copy className="lucide-inline" aria-hidden="true" />
              {i18nT('pages.settings.phoneLoginCard.copy_sign_in_link')}
            </Btn>
            {copied && <span className="text-sm text-ok" role="status">{i18nT('pages.settings.phoneLoginCard.link_copied')}</span>}
          </div>
          <p className="mt-3 text-sm leading-relaxed text-muted">
            {i18nT('pages.settings.phoneLoginCard.send_the_copied_link_to_your_phone_then_open_it')}
          </p>
        </div>
      )}
      {createLink.isError && (
        <p className="mt-3 text-sm text-danger" role="alert">
          {i18nT('pages.settings.phoneLoginCard.could_not_create_a_sign_in_link_try_again')}
        </p>
      )}
      {copyFailed && (
        <p className="mt-3 text-sm text-danger" role="alert">
          {i18nT('pages.settings.phoneLoginCard.copy_failed_select_the_link_and_copy_it_manually')}
        </p>
      )}
    </Card>
  )
}
