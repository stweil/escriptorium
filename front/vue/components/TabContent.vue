<template>
    <div class="row">
        <div class="col-sides">
            <a id="next-part"
               v-if="$store.state.document.readDirection == 'rtl' && $store.state.parts.next"
               href="#"
               @click="getNext"
               class="nav-btn nav-next"
               title="Next (Page Down or Ctrl+Left Arrow)">
                <i class="fas fa-angle-left"></i></a>
            <a id="prev-part"
               v-else-if="$store.state.document.readDirection != 'rtl' && $store.state.parts.previous"
               href="#"
               @click="getPrevious"
               class="nav-btn nav-prev"
               title="Previous (Page Up or Ctrl+Right Arrow)">
                <i class="fas fa-angle-left"></i></a>
            <div>
                <button id="zoom-reset"
                        @click="resetZoom"
                        class="btn btn-sm btn-info"
                        title="Reset zoom. (Ctrl+Backspace)">
                    <i class="fas fa-search-minus"></i>
                </button>
                <input id="zoom-range"
                       type="range"
                       name="zoom-range"
                       class="form-control-range mt-1"
                       orient="vertical"
                       v-bind:min="zoom.minScale"
                       v-bind:max="zoom.maxScale"
                       v-model="zoomScale"
                       step="0.3">
            </div>
        </div>

        <SourcePanel v-if="visible_panels.source && $store.state.parts.loaded"
                     v-bind:fullsizeimage="fullsizeimage"
                     ref="sourcePanel">
        </SourcePanel>

        <keep-alive>
            <SegPanel v-if="visible_panels.segmentation && $store.state.parts.loaded"
                               v-bind:fullsizeimage="fullsizeimage"
                               id="segmentation-panel"
                               ref="segPanel">
            </SegPanel>
        </keep-alive>

        <keep-alive>
            <VisuPanel v-if="visible_panels.visualisation && $store.state.parts.loaded"
                    id="transcription-panel"
                    ref="visuPanel">
            </VisuPanel>
        </keep-alive>

        <keep-alive>
            <DiploPanel id="diplo-panel"
                        v-if="visible_panels.diplomatic && $store.state.parts.loaded"
                        ref="diploPanel">
            </DiploPanel>
        </keep-alive>

        <div class="col-sides">
            <a id="prev-part"
            v-if="$store.state.document.readDirection == 'rtl' && $store.state.parts.previous"
            @click="getPrevious"
            href="#"
            class="nav-btn nav-prev"
            title="Previous (Page Up or Ctrl+Left Arrow)">
                <i class="fas fa-angle-right"></i>
            </a>
            <a id="next-part"
            v-else-if="$store.state.document.readDirection != 'rtl' && $store.state.parts.next"
            @click="getNext"
            href="#"
            class="nav-btn nav-next"
            title="Next (Page Down or Ctrl+Right Arrow)">
                <i class="fas fa-angle-right"></i>
            </a>
        </div>
    </div>
</template>

<script>
import SourcePanel from './SourcePanel.vue';
import SegPanel from './SegPanel.vue';
import VisuPanel from './VisuPanel.vue';
import DiploPanel from './DiploPanel.vue';

export default {
    data: function() {
        return {
            zoom: new WheelZoom(),
            fullsizeimage: false,
        };
    },
    components: {
        SourcePanel,
        SegPanel,
        VisuPanel,
        DiploPanel,
    },
    created() {
        document.addEventListener('keydown', function(event) {
            if (this.$store.state.document.blockShortcuts) return;
            if (event.keyCode == 8 && event.ctrlKey) {  // backspace
                 this.zoom.reset();
            }
        }.bind(this));

        // load the full size image when we reach a scale > 1
        this.zoom.events.addEventListener('wheelzoom.updated', function(ev) {
            if (this.$store.state.parts.loaded && !this.fullsizeimage) {
                let ratio = ev.target.clientWidth / this.$store.state.parts.image.size[0];
                if (this.zoom.scale  * ratio > 1) {
                    this.prefetchImage(this.$store.state.parts.image.uri, function() {
                        this.fullsizeimage = true;
                    }.bind(this));
                }
            }
        }.bind(this));
    },
    computed: {
        visible_panels() {
            return this.$store.state.document.visible_panels;
        },
        openedPanels() {
            return [this.visible_panels.source,
                    this.visible_panels.segmentation,
                    this.visible_panels.visualisation].filter(p=>p===true);
        },
        zoomScale: {
            get() {
                return this.zoom.scale || 1;
            },
            set(newValue) {
                let target = {
                    x: this.$parent.$el.clientWidth/this.openedPanels.length/2-this.zoom.pos.x,
                    y: this.$parent.$el.clientHeight/this.openedPanels.length/2-this.zoom.pos.y
                };
                this.zoom.zoomTo(target, parseFloat(newValue)-this.zoom.scale);
            }
        }
    },
    methods: {
        prefetchImage(src, callback) {
            // this is the panel's responsibility to call this!
            let img = new Image();
            img.addEventListener('load', function() {
                if (callback) callback(src);
                img.remove();
            }.bind(this));
            img.src = src;
        },
        resetZoom() {
            this.zoom.reset();
        },
        async getPrevious(ev) {
            await this.$store.dispatch('parts/loadPart', 'previous');
        },
        async getNext(ev) {
            await this.$store.dispatch('parts/loadPart', 'next');
        },
    }
}
</script>

<style scoped>
</style>
