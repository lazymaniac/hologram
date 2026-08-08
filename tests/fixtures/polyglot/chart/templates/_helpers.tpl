{{- define "pricing.fullname" -}}
{{ .Release.Name }}-pricing
{{- end }}
{{- define "pricing.labels" -}}
app: pricing
{{- end }}
