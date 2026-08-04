from django import forms

from .models import QuestionnaireModule


class QuestionnaireModuleUploadForm(forms.ModelForm):
    class Meta:
        model = QuestionnaireModule
        fields = ["name", "version", "uploaded_file"]

    def clean_uploaded_file(self):
        uploaded_file = self.cleaned_data["uploaded_file"]
        lower_name = uploaded_file.name.lower()

        if not (lower_name.endswith(".json") or lower_name.endswith(".xml")):
            raise forms.ValidationError("Only JSON or XML files are allowed.")

        if uploaded_file.size > 200 * 1024 * 1024:
            raise forms.ValidationError("File is too large. Maximum allowed size is 200 MB.")

        return uploaded_file