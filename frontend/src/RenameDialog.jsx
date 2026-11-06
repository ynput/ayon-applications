import { useState } from 'react'
import { Button, Dialog } from '@ynput/ayon-react-components'

import FilenameInput, { splitFilename, validateBaseName } from './FilenameInput'

// Calls 'onRename' with the new filename (extension is kept), 'onClose' when cancelled
const RenameDialog = ({ filename, existingNames, onRename, onClose }) => {
  const { base, ext } = splitFilename(filename)
  const [baseName, setBaseName] = useState(base)

  const newFilename = baseName.trim() + ext
  let error = validateBaseName(baseName)
  if (!error && newFilename !== filename && existingNames.includes(newFilename)) {
    error = 'An icon with this name already exists'
  }
  const disabled = !!error || newFilename === filename

  return (
    <Dialog
      isOpen
      size="sm"
      header={`Rename ${filename}`}
      onClose={onClose}
      footer={
        <Button
          label="Rename"
          variant="filled"
          disabled={disabled}
          onClick={() => onRename(newFilename)}
        />
      }
    >
      <FilenameInput
        autoFocus
        base={baseName}
        ext={ext}
        onChange={setBaseName}
        error={error}
        warning="Settings that use the current filename have to be updated."
      />
    </Dialog>
  )
}

export default RenameDialog
