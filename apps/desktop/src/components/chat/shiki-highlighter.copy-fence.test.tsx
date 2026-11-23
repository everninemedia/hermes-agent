import { act, cleanup, fireEvent, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type * as ClipboardModule from '@/lib/clipboard'

const { written } = vi.hoisted(() => ({ written: [] as string[] }))

vi.mock('@/lib/clipboard', async importOriginal => ({
  ...(await importOriginal<typeof ClipboardModule>()),
  writeClipboardText: vi.fn(async (text: string) => {
    written.push(text)
  })
}))

import { MarkdownTextContent } from '@/components/assistant-ui/markdown-text'
import { HANDOVER_BLOCK } from '@/lib/copy-fence.fixture'

afterEach(() => {
  cleanup()
  written.length = 0
})

describe('```copy renders as a copyable card', () => {
  it('shows a code card with a visible copy button that copies the raw block', async () => {
    const text = `Hier die Übergabe:\n\n\`\`\`copy\n${HANDOVER_BLOCK}\n\`\`\`\n\nDanach weiter.`
    const { container } = render(<MarkdownTextContent isRunning={false} text={text} />)
    await act(() => new Promise(resolve => setTimeout(resolve, 50)))

    const card = container.querySelector('[data-copy-fence="true"]')
    expect(card).not.toBeNull()
    expect(container.querySelector('.aui-prose-fence')).toBeNull()
    expect(card?.textContent).toContain('Übernimm den Branch')

    const button = card?.querySelector('button')
    expect(button).not.toBeNull()
    expect(button?.className).not.toContain('opacity-0')

    await act(async () => {
      fireEvent.click(button!)
      await new Promise(resolve => setTimeout(resolve, 10))
    })
    expect(written).toEqual([HANDOVER_BLOCK])
  })

  it('keeps unwrapping the same prose in a ```text fence (regression)', async () => {
    const { container } = render(
      <MarkdownTextContent isRunning={false} text={`\`\`\`text\n${HANDOVER_BLOCK}\n\`\`\``} />
    )

    await act(() => new Promise(resolve => setTimeout(resolve, 50)))
    expect(container.querySelector('[data-copy-fence="true"]')).toBeNull()
    expect(container.querySelector('[data-slot="code-card"]')).toBeNull()
  })
})
