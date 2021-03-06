const LineVersion = Vue.extend({
    props: ['version', 'previous'],
    created() {
        this.timeZone = this.$parent.timeZone;
    },
    beoforeDestroy() {
        this.timeZone = null;  // make sure it's garbage collected
    },
    computed: {
        momentDate() {
            return moment.tz(this.version.created_at, this.timeZone).calendar();
        },
        versionContent() {
            if (this.version.data) {
                return this.version.data.content;
            }
        },
        versionCompare() {
            if (this.version.data) {
                if (!this.previous) return this.version.data.content;
                let diff = Diff.diffChars(this.previous.data.content, this.version.data.content);
                return diff.map(function(part){
                    if (part.removed) {
                        return '<span class="cmp-del">'+part.value+'</span>';
                    } else if (part.added) {
                        return '<span class="cmp-add">'+part.value+'</span>';
                    } else {
                        return part.value;
                    }
                }.bind(this)).join('');
            }
        }
    },
    methods: {
        loadState() {
            this.$parent.$emit('update:transcription:version', this.version);
        },
    }
});
