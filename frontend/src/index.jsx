import axios from 'axios'
import React, { useContext, useEffect, useState } from 'react'
import ReactDOM from 'react-dom/client'
import { AddonProvider, AddonContext } from '@ynput/ayon-react-addon-provider'

import '@ynput/ayon-react-components/dist/style.css'

import CustomIcons from './CustomIcons'

const ADDON_NAME = 'applications'

// Fonts are not bundled (see vite.config.js), reuse font faces of AYON frontend
// which hosts this page in a same-origin iframe
const copyParentFontFaces = () => {
  let parentSheets
  try {
    if (window.parent === window) return
    parentSheets = window.parent.document.styleSheets
  } catch {
    return
  }
  const rules = []
  for (const sheet of parentSheets) {
    try {
      for (const rule of sheet.cssRules) {
        if (rule instanceof window.parent.CSSFontFaceRule) rules.push(rule.cssText)
      }
    } catch {
      // Cross-origin stylesheet
    }
  }
  if (!rules.length) return
  const style = document.createElement('style')
  style.textContent = rules.join('\n')
  document.head.appendChild(style)
}

copyParentFontFaces()

const App = () => {
  const context = useContext(AddonContext)
  const accessToken = context?.accessToken
  const addonVersion = context?.addonVersion
  const [tokenSet, setTokenSet] = useState(false)

  useEffect(() => {
    if (accessToken && !tokenSet) {
      axios.defaults.headers.common['Authorization'] = `Bearer ${accessToken}`
      setTokenSet(true)
    }
  }, [accessToken, tokenSet])

  if (!tokenSet || !addonVersion) {
    return null
  }

  const baseUrl = `/api/addons/${ADDON_NAME}/${addonVersion}`
  return <CustomIcons baseUrl={baseUrl} />
}

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <AddonProvider
      devConfig={{
        addonName: ADDON_NAME,
        addonVersion: import.meta.env.VITE_ADDON_VERSION,
        scope: 'settings',
      }}
    >
      <App />
    </AddonProvider>
  </React.StrictMode>,
)
