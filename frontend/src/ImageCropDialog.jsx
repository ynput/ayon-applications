import { useEffect, useRef, useState } from 'react'
import { Button, Dialog } from '@ynput/ayon-react-components'
import styled from 'styled-components'

export const MAX_ICON_SIZE = 512

const MIN_SELECTION_SIZE = 16
const VIEW_MAX_WIDTH = 520
const VIEW_MAX_HEIGHT = 400
// Formats the canvas can write back without changing the filename
const KEEP_FORMATS = ['image/png', 'image/jpeg', 'image/webp']

// Raster formats that can be drawn to canvas (svg and ico are uploaded as they are)
const CROPPABLE_EXTENSIONS = ['png', 'jpg', 'jpeg', 'gif', 'webp']

export const isCroppable = (filename) =>
  CROPPABLE_EXTENSIONS.includes(filename.split('.').pop().toLowerCase())

export const loadImage = (file) =>
  new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file)
    const image = new Image()
    image.onload = () => resolve({ image, url })
    image.onerror = () => {
      URL.revokeObjectURL(url)
      reject(new Error(`Failed to read image ${file.name}`))
    }
    image.src = url
  })

export const needsCrop = (image) =>
  image.naturalWidth !== image.naturalHeight || image.naturalWidth > MAX_ICON_SIZE

const clamp = (value, min, max) => Math.min(Math.max(value, min), max)

// Largest centered square
const getDefaultSelection = (width, height) => {
  const size = Math.min(width, height)
  return {
    x: Math.round((width - size) / 2),
    y: Math.round((height - size) / 2),
    size,
  }
}

const cropFile = (file, image, selection) =>
  new Promise((resolve, reject) => {
    const outputSize = Math.min(selection.size, MAX_ICON_SIZE)
    const canvas = document.createElement('canvas')
    canvas.width = outputSize
    canvas.height = outputSize
    const ctx = canvas.getContext('2d')
    ctx.imageSmoothingEnabled = true
    ctx.imageSmoothingQuality = 'high'
    ctx.drawImage(
      image,
      selection.x,
      selection.y,
      selection.size,
      selection.size,
      0,
      0,
      outputSize,
      outputSize,
    )

    let type = file.type
    let filename = file.name
    if (!KEEP_FORMATS.includes(type)) {
      type = 'image/png'
      filename = filename.replace(/\.[^.]+$/, '') + '.png'
    }
    canvas.toBlob(
      (blob) => {
        if (!blob) {
          reject(new Error(`Failed to crop ${file.name}`))
          return
        }
        resolve(new File([blob], filename, { type }))
      },
      type,
      0.95,
    )
  })

const View = styled.div`
  position: relative;
  overflow: hidden;
  margin: 0 auto;
  user-select: none;
  touch-action: none;
  background: repeating-conic-gradient(#808080 0% 25%, #a0a0a0 0% 50%) 50% / 16px 16px;

  img {
    display: block;
    width: 100%;
    height: 100%;
    pointer-events: none;
  }
`

const Selection = styled.div`
  position: absolute;
  box-sizing: border-box;
  border: 1px solid #fff;
  box-shadow: 0 0 0 9999px rgba(0, 0, 0, 0.6);
  cursor: move;
`

const Handle = styled.div`
  position: absolute;
  right: -7px;
  bottom: -7px;
  width: 14px;
  height: 14px;
  background-color: #fff;
  border: 1px solid #000;
  cursor: nwse-resize;
`

const Info = styled.p`
  margin: 8px 0 0 0;
  text-align: center;
  color: var(--md-sys-color-outline);
`

const Footer = styled.div`
  display: flex;
  gap: 8px;
  justify-content: flex-end;
`

// 'request' is { file, image, url, title, allowOriginal }, the result goes to 'onDone':
// a cropped File, the original File, or null when cancelled
const ImageCropDialog = ({ request, onDone }) => {
  const { file, image, url } = request
  const width = image.naturalWidth
  const height = image.naturalHeight
  const scale = Math.min(1, VIEW_MAX_WIDTH / width, VIEW_MAX_HEIGHT / height)

  const [selection, setSelection] = useState(() => getDefaultSelection(width, height))
  const [working, setWorking] = useState(false)
  const [error, setError] = useState(null)
  const dragRef = useRef(null)

  useEffect(() => () => URL.revokeObjectURL(url), [url])

  const onPointerDown = (event, mode) => {
    event.preventDefault()
    event.stopPropagation()
    event.currentTarget.setPointerCapture(event.pointerId)
    dragRef.current = {
      mode,
      startX: event.clientX,
      startY: event.clientY,
      start: selection,
    }
  }

  const onPointerMove = (event) => {
    const drag = dragRef.current
    if (!drag) return
    const dx = (event.clientX - drag.startX) / scale
    const dy = (event.clientY - drag.startY) / scale
    const { start } = drag
    if (drag.mode === 'move') {
      setSelection({
        ...start,
        x: Math.round(clamp(start.x + dx, 0, width - start.size)),
        y: Math.round(clamp(start.y + dy, 0, height - start.size)),
      })
    } else {
      const maxSize = Math.min(width - start.x, height - start.y)
      const size = Math.round(
        clamp(start.size + Math.max(dx, dy), Math.min(MIN_SELECTION_SIZE, maxSize), maxSize),
      )
      setSelection({ ...start, size })
    }
  }

  const onPointerUp = () => {
    dragRef.current = null
  }

  const onCrop = async () => {
    setWorking(true)
    try {
      onDone(await cropFile(file, image, selection))
    } catch (err) {
      setError(err.message)
      setWorking(false)
    }
  }

  const outputSize = Math.min(selection.size, MAX_ICON_SIZE)

  return (
    <Dialog
      isOpen
      size="lg"
      header={request.title || `Crop ${file.name}`}
      onClose={() => onDone(null)}
      footer={
        <Footer>
          <Button
            label="Reset"
            variant="text"
            onClick={() => setSelection(getDefaultSelection(width, height))}
          />
          {request.allowOriginal && (
            <Button label="Use original" onClick={() => onDone(file)} disabled={working} />
          )}
          <Button label="Crop" variant="filled" onClick={onCrop} disabled={working} />
        </Footer>
      }
    >
      <View
        style={{ width: width * scale, height: height * scale }}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
      >
        <img src={url} alt={file.name} draggable={false} />
        <Selection
          style={{
            left: selection.x * scale,
            top: selection.y * scale,
            width: selection.size * scale,
            height: selection.size * scale,
          }}
          onPointerDown={(event) => onPointerDown(event, 'move')}
        >
          <Handle onPointerDown={(event) => onPointerDown(event, 'resize')} />
        </Selection>
      </View>
      <Info>
        Image {width}×{height} px, selection {selection.size}×{selection.size} px, result{' '}
        {outputSize}×{outputSize} px
      </Info>
      {error && <Info style={{ color: 'var(--md-sys-color-error)' }}>{error}</Info>}
    </Dialog>
  )
}

export default ImageCropDialog
